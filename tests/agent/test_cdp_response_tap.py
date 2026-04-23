"""Unit tests for agent.browser.cdp_response_tap.CDPResponseTap.

Uses a FakeCDPSession and FakeSink to exercise the tap's CDP event state
machine without a real browser.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import pytest

from agent.browser.cdp_response_tap import CDPResponseTap


# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeCDPSession:
    """Minimal CDP session stub."""

    def __init__(self, fail_get_body: bool = False, fail_with: str | None = None) -> None:
        self._handlers: dict[str, list] = {}
        self.sent: list[tuple[str, dict]] = []
        self._fail_get_body = fail_get_body
        self._fail_with = fail_with
        self.detached = False
        self._body_store: dict[str, tuple[bytes, bool]] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method == "Network.enable":
            return {}
        if method == "Network.getResponseBody":
            request_id = (params or {}).get("requestId", "")
            if self._fail_get_body:
                msg = self._fail_with or "No resource with given identifier found"
                raise RuntimeError(msg)
            body_bytes, b64 = self._body_store.get(request_id, (b"<html/>", False))
            if b64:
                return {"body": base64.b64encode(body_bytes).decode(), "base64Encoded": True}
            return {"body": body_bytes.decode("utf-8", errors="replace"), "base64Encoded": False}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def fire(self, event: str, payload: dict) -> None:
        for h in list(self._handlers.get(event, [])):
            result = h(payload)
            if asyncio.iscoroutine(result):
                asyncio.get_event_loop().run_until_complete(result)

    def set_body(self, request_id: str, body: bytes, b64: bool = False) -> None:
        self._body_store[request_id] = (body, b64)


class FakeContext:
    """Minimal context stub that returns one pre-built page."""

    def __init__(self, session: FakeCDPSession) -> None:
        self._session = session
        self._handlers: dict[str, list] = {}
        self.pages: list[FakePage] = [FakePage(session)]

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        try:
            self._handlers[event].remove(handler)
        except (KeyError, ValueError):
            pass

    async def new_cdp_session(self, page: Any) -> FakeCDPSession:
        return self._session


class FakePage:
    def __init__(self, session: FakeCDPSession) -> None:
        self._session = session


class FakeSink:
    """Collects record_response / record_failure calls for assertion."""

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.failures: list[dict] = []

    async def record_response(self, **kwargs: Any) -> None:
        self.responses.append(kwargs)

    async def record_failure(self, **kwargs: Any) -> None:
        self.failures.append(kwargs)


# ── Helpers ────────────────────────────────────────────────────────────────


def _request_sent(url: str, request_id: str = "r1", method: str = "GET") -> dict:
    return {
        "requestId": request_id,
        "request": {"url": url, "method": method},
        "type": "Document",
        "frameId": "f1",
    }


def _response_received(request_id: str = "r1", status: int = 200, mime: str = "text/html") -> dict:
    return {
        "requestId": request_id,
        "response": {
            "status": status,
            "mimeType": mime,
            "headers": {"content-type": mime},
            "remoteIPAddress": "1.2.3.4",
            "remotePort": 443,
        },
    }


def _loading_finished(request_id: str = "r1") -> dict:
    return {"requestId": request_id}


def _loading_failed(request_id: str = "r1", error: str = "net::ERR_FAILED", canceled: bool = False) -> dict:
    return {"requestId": request_id, "errorText": error, "canceled": canceled}


# ── Tests ──────────────────────────────────────────────────────────────────


async def test_happy_path_body_written(tmp_path):
    """loadingFinished with a valid body → record_response called with outcome ok."""
    session = FakeCDPSession()
    body = b"<html>hello</html>"
    session.set_body("r1", body)

    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/"))
    session.fire("Network.responseReceived", _response_received())
    await asyncio.sleep(0)  # let scheduled task start
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    assert len(sink.responses) == 1
    r = sink.responses[0]
    assert r["outcome"] == "ok"
    assert r["url"] == "https://example.com/"
    assert r["body"] == body
    assert r["status"] == 200
    assert r["remote_address"] == "1.2.3.4:443"


async def test_base64_body_decoded(tmp_path):
    """base64Encoded=True path decodes correctly."""
    session = FakeCDPSession()
    body = b"\x00\x01\x02binary"
    session.set_body("r1", body, b64=True)

    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/img.png"))
    session.fire("Network.responseReceived", _response_received(mime="image/png"))
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    assert sink.responses[0]["body"] == body


async def test_getresponsebody_not_found_yields_disposed():
    """'No resource with given identifier' → record_failure with outcome disposed."""
    session = FakeCDPSession(fail_get_body=True)
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/"))
    session.fire("Network.responseReceived", _response_received())
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    assert len(sink.failures) == 1
    assert sink.failures[0]["outcome"] == "disposed"
    assert len(sink.responses) == 0


async def test_loading_failed_canceled_yields_aborted():
    session = FakeCDPSession()
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/"))
    session.fire("Network.loadingFailed", _loading_failed(canceled=True))
    await tap.drain()

    assert sink.failures[0]["outcome"] == "aborted"


async def test_loading_failed_error_yields_failed():
    session = FakeCDPSession()
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/"))
    session.fire("Network.loadingFailed", _loading_failed(error="net::ERR_NAME_NOT_RESOLVED", canceled=False))
    await tap.drain()

    assert sink.failures[0]["outcome"] == "failed"


async def test_redirect_chain_emits_redirect_then_ok():
    """Redirect: requestWillBeSent with redirectResponse → synthetic redirect entry, then ok for final."""
    session = FakeCDPSession()
    body = b"<html>final</html>"
    session.set_body("r1", body)

    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    # First hop
    session.fire("Network.requestWillBeSent", _request_sent("https://a.example/"))
    session.fire("Network.responseReceived", _response_received(status=200))

    # Second hop — reuses request_id, carries redirectResponse for first hop
    second_sent = {
        "requestId": "r1",
        "request": {"url": "https://b.example/", "method": "GET"},
        "type": "Document",
        "frameId": "f1",
        "redirectResponse": {
            "status": 302,
            "mimeType": "text/html",
            "headers": {},
        },
    }
    session.fire("Network.requestWillBeSent", second_sent)
    session.fire("Network.responseReceived", _response_received(status=200))
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    # Expect one redirect entry + one ok entry
    outcomes = {r["outcome"] for r in sink.responses}
    assert "redirect" in outcomes
    assert "ok" in outcomes
    # request_ids must be distinct so manifest rows are unique
    ids = [r["request_id"] for r in sink.responses]
    assert len(set(ids)) == 2


async def test_detach_called_during_drain():
    """drain() must call detach() on every attached session."""
    session = FakeCDPSession()
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)
    await tap.drain()

    assert session.detached is True


async def test_drain_is_idempotent():
    session = FakeCDPSession()
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)
    await tap.drain()
    await tap.drain()  # must not raise


async def test_data_url_ignored():
    """Requests with data: URLs are silently skipped."""
    session = FakeCDPSession()
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("data:text/html,<h1>hi</h1>"))
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    assert sink.responses == []
    assert sink.failures == []


async def test_getresponsebody_called_exactly_once_on_loading_finished():
    """Network.getResponseBody must be called exactly once per loadingFinished."""
    session = FakeCDPSession()
    session.set_body("r1", b"content")
    sink = FakeSink()
    tap = CDPResponseTap(sink=sink)
    ctx = FakeContext(session)
    await tap.attach_to_context(ctx)

    session.fire("Network.requestWillBeSent", _request_sent("https://example.com/"))
    session.fire("Network.responseReceived", _response_received())
    session.fire("Network.loadingFinished", _loading_finished())
    await tap.drain()

    body_fetches = [s for s in session.sent if s[0] == "Network.getResponseBody"]
    assert len(body_fetches) == 1
