"""CDP Fetch-domain response interceptor for main-frame documents.

``Network.getResponseBody`` loses bodies for fast-navigating documents
(parser-blocked inline ``<script>`` that calls ``location.href = ...``)
because Chromium disposes the response buffer as soon as the new
navigation commits — the body is gone before our async
``getResponseBody`` call returns.

The ``Fetch`` domain, by contrast, pauses each matching response at the
network layer *before* the renderer sees it.  While paused we can call
``Fetch.getResponseBody`` reliably (the resource is held open for the
duration of the pause), then ``Fetch.continueResponse`` to let the page
proceed normally.  Scoped to ``resourceType: "Document"`` so we only
touch main-frame and iframe navigations — subresources continue to flow
through the existing ``CDPResponseTap`` (``Network.getResponseBody``)
path, which doesn't race for those.

Invariant: every ``Fetch.requestPaused`` event MUST be resolved with
``Fetch.continueResponse`` (or ``fulfillRequest`` / ``failRequest``).
A leaked pause hangs the navigation, so even on body-read failure we
still call ``continueResponse``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.browser.network_capture import NetworkCapture

logger = logging.getLogger(__name__)


class CDPFetchInterceptor:
    """Per-context Fetch-domain interceptor for Document responses.

    Usage::

        interceptor = CDPFetchInterceptor(sink=capture)
        await interceptor.attach_to_context(context)
        # ... detonation runs ...
        await interceptor.drain()
    """

    def __init__(self, sink: NetworkCapture) -> None:
        self._sink = sink
        self._sessions: list[Any] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._context: Any = None
        self._drained = False

    # ── Public interface ──────────────────────────────────────────

    async def attach_to_context(self, context: Any) -> None:
        """Subscribe to page-attach events and attach to all existing pages."""
        self._context = context
        context.on("page", self._on_page)
        for page in context.pages:
            await self._attach_page(page)

    async def drain(self) -> None:
        """Disable Fetch on every session and flush in-flight pauses.

        Idempotent — safe to call more than once.  ``Fetch.disable``
        releases any paused requests Chromium was holding for us, so
        navigations never stall on drain.
        """
        if self._drained:
            return
        self._drained = True

        if self._context is not None:
            try:
                self._context.remove_listener("page", self._on_page)
            except Exception:
                pass

        for session in list(self._sessions):
            try:
                await asyncio.wait_for(session.send("Fetch.disable"), timeout=2.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(session.detach(), timeout=2.0)
            except Exception:
                pass

        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── Internal page attachment ──────────────────────────────────

    def _on_page(self, page: Any) -> None:
        asyncio.create_task(self._attach_page(page))

    async def _attach_page(self, page: Any) -> None:
        try:
            session = await self._context.new_cdp_session(page)
        except Exception as exc:
            logger.debug("fetch_tap: new_cdp_session failed: %s", exc)
            return
        self._sessions.append(session)

        session.on(
            "Fetch.requestPaused",
            lambda ev: self._schedule(self._on_request_paused(ev, session)),
        )

        try:
            await session.send(
                "Fetch.enable",
                {
                    "patterns": [
                        {"resourceType": "Document", "requestStage": "Response"}
                    ]
                },
            )
        except Exception as exc:
            logger.debug("fetch_tap: Fetch.enable failed: %s", exc)

    def _schedule(self, coro: Any) -> None:
        """Schedule a paused-request handler.

        IMPORTANT: unlike ``CDPResponseTap._schedule``, we MUST NOT drop
        events when drained.  Every ``Fetch.requestPaused`` we receive
        owns a paused request that must be released with
        ``Fetch.continueResponse`` — if we silently drop the event, the
        navigation hangs until the browser tears down.  ``drain()``
        stops new events at the source via ``Fetch.disable``.
        """
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── CDP event handler ────────────────────────────────────────

    async def _on_request_paused(self, event: dict[str, Any], session: Any) -> None:
        """Read the paused response body and release the pause.

        Order is deliberate: attempt body read first, then ALWAYS call
        ``continueResponse`` regardless of outcome.  Sink recording
        happens last so a slow manifest write cannot delay navigation.
        """
        fetch_id = event.get("requestId", "")  # Fetch interception ID
        network_id = event.get("networkId", "")  # correlates with Network domain
        request = event.get("request", {})
        url = request.get("url", "")
        method = (request.get("method") or "GET").upper()
        status = event.get("responseStatusCode", 0)
        resource_type = (event.get("resourceType") or "").lower() or None

        headers_list = event.get("responseHeaders") or []
        headers: dict[str, str] = {}
        for h in headers_list:
            name = (h.get("name") or "").lower()
            if name:
                headers[name] = h.get("value", "")
        mime_type = (headers.get("content-type", "").split(";", 1)[0].strip()) or None

        # data: URLs shouldn't hit Fetch.enable patterns in practice, but
        # guard anyway so we never leak a paused request.
        if url.startswith("data:"):
            try:
                await session.send("Fetch.continueResponse", {"requestId": fetch_id})
            except Exception:
                pass
            return

        body_bytes: bytes | None = None
        outcome: str = "ok"
        reason: str | None = None

        try:
            result = await session.send(
                "Fetch.getResponseBody", {"requestId": fetch_id}
            )
            if result.get("base64Encoded"):
                try:
                    body_bytes = base64.b64decode(result.get("body", ""))
                except Exception as exc:
                    outcome = "error"
                    reason = f"base64 decode failed: {exc}"
            else:
                body_bytes = (result.get("body") or "").encode(
                    "utf-8", errors="replace"
                )
        except Exception as exc:
            logger.debug("fetch_tap: getResponseBody failed for %s: %s", url, exc)
            outcome = "error"
            reason = str(exc)

        # CRITICAL: always release the pause, even on body-read failure.
        try:
            await session.send("Fetch.continueResponse", {"requestId": fetch_id})
        except Exception as exc:
            logger.debug("fetch_tap: continueResponse failed for %s: %s", url, exc)

        # Use networkId as the sink's request_id so downstream chain-walk
        # bookkeeping (which keys off Network-domain requestIds) correlates
        # Fetch-captured docs with redirect-hop entries from CDPResponseTap.
        tagged_id = network_id or fetch_id

        if body_bytes is not None and outcome == "ok":
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
                outcome=outcome,  # type: ignore[arg-type]
                reason=reason,
            )
