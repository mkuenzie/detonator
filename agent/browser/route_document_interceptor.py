"""Route-based Document response interceptor.

Replaces the per-page ``Fetch.enable`` CDP interceptor, which opened a second
CDP session per page and shadowed patchright's own ``Fetch.enable`` channel —
two sessions racing on the same paused responses would silently stall cross-
origin navigations that happened mid-detonation (e.g. a click handler doing
``window.location.href = 'http://...'`` from a detached modal).

``context.route()`` goes through the same CDP client Playwright/patchright
already owns, so there is no second session to conflict with. The trade-off
is that handler order matters: patchright registers its own route for
``http://patchright-init-script-inject.internal/`` (the stealth-bundle
injection channel, which avoids ``Runtime.enable`` detection). Playwright
matches handlers in reverse registration order, so ours fires first on every
request — any URL we do not own MUST be passed through via ``route.fallback()``
(not ``continue_()``), or patchright's injection never runs.

Redirect handling: ``route.fetch(max_redirects=0)`` returns the 3xx response
verbatim. We fulfill with it, the browser follows the ``Location`` header,
and the next hop fires our handler again — so every hop in a redirect chain
is recorded as its own manifest entry.

Scope: only ``resource_type == "document"`` runs through the fetch/fulfill
path. Subresources fall back to ``CDPResponseTap`` (``Network.getResponseBody``),
which doesn't race for them.

Invariant: every intercepted route MUST be resolved (``fulfill`` or
``fallback`` or ``abort``). A leaked route hangs the navigation.
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from agent.browser.network_capture import NetworkCapture

logger = logging.getLogger(__name__)

# Hostnames we must NOT intercept — their routes belong to patchright (or any
# other registered route handler). Falling back lets those handlers fulfill.
_RESERVED_HOST_SUFFIXES: tuple[str, ...] = (".internal",)


class RouteDocumentInterceptor:
    """Captures main-frame and iframe Document response bodies via ``context.route()``.

    Usage::

        interceptor = RouteDocumentInterceptor(sink=capture)
        await interceptor.attach_to_context(context)
        # ... detonation runs ...
        await interceptor.drain()
    """

    def __init__(self, sink: NetworkCapture) -> None:
        self._sink = sink
        self._context: Any = None
        self._attached: bool = False
        # Monotonic counter to tag manifest entries uniquely per hop; Playwright
        # Request objects don't expose a stable CDP requestId we can share with
        # CDPResponseTap, so we use URL#seq as the manifest key.
        self._seq = itertools.count()

    # ── Public interface ──────────────────────────────────────────

    async def attach_to_context(self, context: Any) -> None:
        """Register the route handler. Matches every URL; filtering is in the handler."""
        self._context = context
        await context.route("**/*", self._handle)
        self._attached = True

    async def drain(self) -> None:
        """Unregister the route handler. Idempotent."""
        if not self._attached:
            return
        self._attached = False
        try:
            await self._context.unroute("**/*", self._handle)
        except Exception as exc:
            logger.debug("route_doc: unroute failed: %s", exc)

    # ── Route handler ────────────────────────────────────────────

    async def _handle(self, route: Any) -> None:
        """Intercept Document responses; fall back for everything else.

        Every exit path MUST resolve the route.
        """
        request = route.request
        resource_type = request.resource_type
        url = request.url
        method = (request.method or "GET").upper()

        # Non-documents flow to whichever earlier handler (or the default) owns
        # them. CDPResponseTap captures these via Network.getResponseBody.
        if resource_type != "document":
            await self._safe_fallback(route, url)
            return

        # Reserved hosts (patchright's stealth-injection URL) belong to an
        # earlier handler — yielding here keeps stealth working.
        host = (urlparse(url).hostname or "").lower()
        if host.endswith(_RESERVED_HOST_SUFFIXES):
            await self._safe_fallback(route, url)
            return

        # data: URLs have no wire body; defer to the default path.
        if url.startswith("data:"):
            await self._safe_fallback(route, url)
            return

        seq = next(self._seq)
        tagged_id = f"route-doc-{seq}"

        try:
            # max_redirects=0 so each 3xx hop fires our handler again — the
            # browser follows Location after we fulfill, so the chain is
            # naturally walked one hop per invocation.
            response = await route.fetch(max_redirects=0)
        except Exception as exc:
            logger.debug("route_doc: fetch failed for %s: %s", url, exc)
            await self._sink.record_failure(
                request_id=tagged_id,
                url=url,
                method=method,
                outcome="error",
                reason=str(exc),
            )
            # If we can't fetch, fall back so the browser tries its own path
            # rather than hanging the navigation.
            await self._safe_fallback(route, url)
            return

        status = response.status
        raw_headers = response.headers or {}
        headers = {k.lower(): v for k, v in raw_headers.items()}
        mime_type = (headers.get("content-type", "").split(";", 1)[0].strip()) or None

        body_bytes: bytes | None = None
        try:
            body_bytes = await response.body()
        except Exception as exc:
            logger.debug("route_doc: body read failed for %s: %s", url, exc)

        # Record first — even if fulfill fails downstream, we still have the
        # body archived. Ordering mirrors CDPFetchInterceptor.
        if body_bytes is not None:
            await self._sink.record_response(
                request_id=tagged_id,
                url=url,
                method=method,
                status=status,
                mime_type=mime_type,
                resource_type=resource_type,
                frame_url=None,
                remote_address=None,
                response_headers=headers or None,
                body=body_bytes,
                outcome="ok",
            )
        else:
            await self._sink.record_failure(
                request_id=tagged_id,
                url=url,
                method=method,
                outcome="error",
                reason="body read returned None",
            )

        try:
            await route.fulfill(response=response)
        except Exception as exc:
            logger.debug("route_doc: fulfill failed for %s: %s", url, exc)
            # As a last resort, try to release the route without hanging.
            await self._safe_fallback(route, url)

    # ── Helpers ───────────────────────────────────────────────────

    async def _safe_fallback(self, route: Any, url: str) -> None:
        """Fall back, swallowing errors. A route must never be left unresolved."""
        try:
            await route.fallback()
        except Exception as exc:
            logger.debug("route_doc: fallback failed for %s: %s", url, exc)
            # Last-ditch: abort so Chromium frees the paused request.
            try:
                await route.abort()
            except Exception:
                pass
