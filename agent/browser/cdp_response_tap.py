"""CDP Network-domain response tap for per-page body capture.

Attaches a CDP session to each page in a Playwright persistent context and
captures response bodies via ``Network.getResponseBody`` inside the
``loadingFinished`` handler — the only window in which Chromium guarantees the
body is still resident.  This closes the disposal race that makes the
``context.on("response") → await response.body()`` path miss main-frame
document bodies on fast-redirecting pages.

Request-body capture is left entirely to ``NetworkCapture`` (via the Playwright
``context.on("request")`` path) because ``post_data_buffer`` is synchronously
available and has never raced.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.browser.network_capture import NetworkCapture

logger = logging.getLogger(__name__)

_STASH_SOFT_CAP = 5000


class CDPResponseTap:
    """Per-context CDP Network listener that feeds bodies to a NetworkCapture sink.

    Usage::

        tap = CDPResponseTap(sink=capture)
        await tap.attach_to_context(context)
        # ... detonation runs ...
        await tap.drain()
    """

    def __init__(self, sink: NetworkCapture) -> None:
        self._sink = sink
        self._sessions: list[Any] = []
        self._tasks: set[asyncio.Task[None]] = set()
        # requestId → {url, method, resource_type, frame_url, status, mime_type,
        #               headers, remote_address, chain_index}
        # OrderedDict for LRU eviction.
        self._stash: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._context: Any = None
        self._drained = False

    async def attach_to_context(self, context: Any) -> None:
        """Subscribe to page-attach events and attach to all existing pages."""
        self._context = context
        context.on("page", self._on_page)
        for page in context.pages:
            await self._attach_page(page)

    async def drain(self) -> None:
        """Stop accepting new events and flush all in-flight body fetches.

        Idempotent — safe to call more than once.
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
            logger.debug("cdp_tap: new_cdp_session failed: %s", exc)
            return
        self._sessions.append(session)

        session.on("Network.requestWillBeSent", self._on_request_will_be_sent)
        session.on("Network.responseReceived", self._on_response_received)
        session.on("Network.loadingFinished", lambda ev: self._schedule(self._on_loading_finished(ev, session)))
        session.on("Network.loadingFailed", lambda ev: self._schedule(self._on_loading_failed(ev)))

        try:
            await session.send("Network.enable")
        except Exception as exc:
            logger.debug("cdp_tap: Network.enable failed: %s", exc)

    def _schedule(self, coro: Any) -> None:
        if self._drained:
            return
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── CDP event handlers ────────────────────────────────────────

    def _on_request_will_be_sent(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId", "")
        request = event.get("request", {})
        url = request.get("url", "")

        if url.startswith("data:"):
            return

        # Chromium reuses requestId across redirect chains.
        # If we already have a stash entry for this ID, the prior hop redirected.
        next_chain_idx = 0
        if request_id in self._stash:
            prior = self._stash.pop(request_id)
            prior_chain_idx = prior.get("chain_index", 0)
            next_chain_idx = prior_chain_idx + 1
            redirect_resp = event.get("redirectResponse")
            if redirect_resp:
                tagged_id = f"{request_id}#{prior_chain_idx}"
                self._schedule(
                    self._emit_redirect(
                        tagged_id,
                        prior["url"],
                        prior.get("method", "GET"),
                        redirect_resp.get("status", 0),
                        redirect_resp.get("mimeType"),
                        prior.get("resource_type"),
                        prior.get("frame_url"),
                        _remote_addr(redirect_resp),
                        {k.lower(): v for k, v in redirect_resp.get("headers", {}).items()} or None,
                    )
                )

        frame_id = event.get("frameId", "")

        # LRU eviction if stash hits soft cap.
        while len(self._stash) >= _STASH_SOFT_CAP:
            evicted_id, _ = self._stash.popitem(last=False)
            logger.debug("cdp_tap: stash eviction for requestId=%s", evicted_id)

        self._stash[request_id] = {
            "url": url,
            "method": (request.get("method") or "GET").upper(),
            "resource_type": event.get("type", "").lower() or None,
            "frame_url": frame_id,  # resolved to URL on responseReceived if possible
            "status": None,
            "mime_type": None,
            "headers": None,
            "remote_address": None,
            "chain_index": next_chain_idx,
        }
        self._stash.move_to_end(request_id)

    def _on_response_received(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId", "")
        if request_id not in self._stash:
            return
        resp = event.get("response", {})
        entry = self._stash[request_id]
        entry["status"] = resp.get("status", 0)
        entry["mime_type"] = resp.get("mimeType")
        entry["headers"] = {k.lower(): v for k, v in resp.get("headers", {}).items()} or None
        entry["remote_address"] = _remote_addr(resp)
        # frame_url: prefer the actual URL from the response over the frame ID
        frame = event.get("frame", {})
        if frame.get("url"):
            entry["frame_url"] = frame["url"]
        self._stash.move_to_end(request_id)

    async def _on_loading_finished(self, event: dict[str, Any], session: Any) -> None:
        request_id = event.get("requestId", "")
        stash_entry = self._stash.pop(request_id, None)
        if stash_entry is None:
            return

        url = stash_entry["url"]
        if url.startswith("data:"):
            return

        method = stash_entry.get("method", "GET")
        status = stash_entry.get("status") or 0
        mime_type = stash_entry.get("mime_type")
        resource_type = stash_entry.get("resource_type")
        frame_url = stash_entry.get("frame_url")
        remote_address = stash_entry.get("remote_address")
        headers = stash_entry.get("headers")
        chain_idx = stash_entry.get("chain_index", 0)
        tagged_id = f"{request_id}#{chain_idx}" if chain_idx else request_id

        try:
            result = await session.send("Network.getResponseBody", {"requestId": request_id})
        except Exception as exc:
            msg = str(exc).lower()
            if "no resource with given identifier" in msg or "not found" in msg:
                outcome = "disposed"
            else:
                logger.debug("cdp_tap: getResponseBody error for %s: %s", url, exc)
                outcome = "error"
            await self._sink.record_failure(
                request_id=tagged_id, url=url, method=method, outcome=outcome, reason=str(exc)
            )
            return

        raw: bytes
        if result.get("base64Encoded"):
            try:
                raw = base64.b64decode(result.get("body", ""))
            except Exception:
                raw = b""
        else:
            raw = (result.get("body") or "").encode("utf-8", errors="replace")

        await self._sink.record_response(
            request_id=tagged_id,
            url=url,
            method=method,
            status=status,
            mime_type=mime_type,
            resource_type=resource_type,
            frame_url=frame_url,
            remote_address=remote_address,
            response_headers=headers,
            body=raw or None,
            outcome="ok",
        )

    async def _on_loading_failed(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId", "")
        stash_entry = self._stash.pop(request_id, None)
        if stash_entry is None:
            return

        url = stash_entry["url"]
        method = stash_entry.get("method", "GET")
        chain_idx = stash_entry.get("chain_index", 0)
        tagged_id = f"{request_id}#{chain_idx}" if chain_idx else request_id

        if event.get("canceled"):
            outcome = "aborted"
        else:
            outcome = "failed"

        await self._sink.record_failure(
            request_id=tagged_id,
            url=url,
            method=method,
            outcome=outcome,
            reason=event.get("errorText"),
        )

    async def _emit_redirect(
        self,
        tagged_id: str,
        url: str,
        method: str,
        status: int,
        mime_type: str | None,
        resource_type: str | None,
        frame_url: str | None,
        remote_address: str | None,
        response_headers: dict[str, Any] | None,
    ) -> None:
        await self._sink.record_response(
            request_id=tagged_id,
            url=url,
            method=method,
            status=status,
            mime_type=mime_type,
            resource_type=resource_type,
            frame_url=frame_url,
            remote_address=remote_address,
            response_headers=response_headers,
            body=None,
            outcome="redirect",
        )


# ── Helpers ───────────────────────────────────────────────────────


def _remote_addr(resp: dict[str, Any]) -> str | None:
    ip = resp.get("remoteIPAddress")
    port = resp.get("remotePort")
    if ip and port is not None:
        return f"{ip}:{port}"
    return None
