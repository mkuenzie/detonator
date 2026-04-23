"""Unit tests for agent.browser.cdp_fetch_interceptor.CDPFetchInterceptor.

Mirrors the ``FakeCDPSession`` / ``FakeSink`` pattern from
``test_cdp_response_tap.py`` but drives the ``Fetch.requestPaused`` path.

Non-negotiable invariant under test: every ``Fetch.requestPaused`` event
MUST result in exactly one ``Fetch.continueResponse`` call — even on body
read failure, base64 decode failure, or sink errors.  A leaked pause
hangs the navigation, so every failure mode asserts the release.
"""

from __future__ import annotations

import base64
from typing import Any

from agent.browser.cdp_fetch_interceptor import CDPFetchInterceptor

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeCDPSession:
    """Minimal CDP session stub for Fetch-domain events."""

    def __init__(
        self,
        *,
        fail_get_body: bool = False,
        fail_continue: bool = False,
        fail_get_with: str | None = None,
    ) -> None:
        self._handlers: dict[str, list] = {}
        self.sent: list[tuple[str, dict]] = []
        self._fail_get_body = fail_get_body
        self._fail_continue = fail_continue
        self._fail_get_with = fail_get_with
        self.detached = False
        # request_id → (body bytes, base64?)
        self._body_store: dict[str, tuple[bytes, bool]] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method == "Fetch.enable":
            return {}
        if method == "Fetch.disable":
            return {}
        if method == "Fetch.getResponseBody":
            request_id = (params or {}).get("requestId", "")
            if self._fail_get_body:
                msg = self._fail_get_with or "No resource with given identifier found"
                raise RuntimeError(msg)
            body_bytes, b64 = self._body_store.get(request_id, (b"<html/>", False))
            if b64:
                return {"body": base64.b64encode(body_bytes).decode(), "base64Encoded": True}
            return {"body": body_bytes.decode("utf-8", errors="replace"), "base64Encoded": False}
        if method == "Fetch.continueResponse":
            if self._fail_continue:
                raise RuntimeError("continueResponse synthetic failure")
            return {}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def fire(self, event: str, payload: dict) -> None:
        for h in list(self._handlers.get(event, [])):
            h(payload)

    def set_body(self, request_id: str, body: bytes, b64: bool = False) -> None:
        self._body_store[request_id] = (body, b64)

    def continued_ids(self) -> list[str]:
        return [
            p.get("requestId", "")
            for (m, p) in self.sent
            if m == "Fetch.continueResponse"
        ]

    def get_body_ids(self) -> list[str]:
        return [
            p.get("requestId", "")
            for (m, p) in self.sent
            if m == "Fetch.getResponseBody"
        ]


class FakeContext:
    """Context stub that returns a pre-built page and one CDP session."""

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


def _paused_event(
    *,
    request_id: str = "interception-1",
    network_id: str = "net-1",
    url: str = "https://example.com/",
    method: str = "GET",
    status: int = 200,
    resource_type: str = "Document",
    headers: list[dict] | None = None,
) -> dict:
    if headers is None:
        headers = [
            {"name": "content-type", "value": "text/html; charset=utf-8"},
            {"name": "content-length", "value": "42"},
            {"name": "location", "value": "https://next.example/"},
        ]
    return {
        "requestId": request_id,
        "networkId": network_id,
        "request": {"url": url, "method": method},
        "responseStatusCode": status,
        "responseHeaders": headers,
        "resourceType": resource_type,
    }


# ── Tests ──────────────────────────────────────────────────────────────────


async def test_happy_path_body_captured_and_release():
    """Body read succeeds → record_response(ok); continueResponse fires exactly once."""
    session = FakeCDPSession()
    body = b"<html>hello</html>"
    session.set_body("interception-1", body)

    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire("Fetch.requestPaused", _paused_event())
    await interceptor.drain()

    assert len(sink.responses) == 1
    r = sink.responses[0]
    assert r["outcome"] == "ok"
    assert r["body"] == body
    assert r["url"] == "https://example.com/"
    assert r["status"] == 200
    assert r["mime_type"] == "text/html"
    assert r["resource_type"] == "document"
    # request_id must use networkId so it correlates with Network domain
    assert r["request_id"] == "net-1"
    # All headers passed through, lowercased, including 'location' which was
    # excluded by the old allowlist — this is the regression we care about.
    assert r["response_headers"]["location"] == "https://next.example/"
    assert r["response_headers"]["content-type"] == "text/html; charset=utf-8"
    # Exactly one continueResponse per pause
    assert session.continued_ids() == ["interception-1"]


async def test_base64_encoded_body_decoded():
    """base64Encoded responses decode to raw bytes."""
    session = FakeCDPSession()
    body = b"\x00\x01\x02\xff binary payload"
    session.set_body("interception-1", body, b64=True)

    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire("Fetch.requestPaused", _paused_event())
    await interceptor.drain()

    assert sink.responses[0]["body"] == body
    assert session.continued_ids() == ["interception-1"]


async def test_get_body_failure_records_failure_and_still_releases():
    """Invariant: continueResponse must fire even when getResponseBody fails."""
    session = FakeCDPSession(fail_get_body=True)
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire("Fetch.requestPaused", _paused_event())
    await interceptor.drain()

    assert len(sink.responses) == 0
    assert len(sink.failures) == 1
    assert sink.failures[0]["outcome"] == "error"
    assert "No resource" in (sink.failures[0]["reason"] or "")
    # The pause MUST have been released.
    assert session.continued_ids() == ["interception-1"]


async def test_continue_response_failure_is_swallowed():
    """If continueResponse itself fails, we log and move on — no exception escapes."""
    session = FakeCDPSession(fail_continue=True)
    session.set_body("interception-1", b"<html/>")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire("Fetch.requestPaused", _paused_event())
    await interceptor.drain()  # must not raise

    # Body was still recorded successfully — the continueResponse failure
    # happens after the body read, so the sink write still proceeds.
    assert len(sink.responses) == 1
    assert sink.responses[0]["outcome"] == "ok"


async def test_data_url_short_circuits_but_still_releases():
    """data: URLs skip sink writes but must still release the pause."""
    session = FakeCDPSession()
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire(
        "Fetch.requestPaused",
        _paused_event(url="data:text/html,<h1>hi</h1>"),
    )
    await interceptor.drain()

    assert sink.responses == []
    assert sink.failures == []
    # data: short-circuit must not skip the release.
    assert session.continued_ids() == ["interception-1"]
    # And we must NOT have called getResponseBody for it.
    assert session.get_body_ids() == []


async def test_fetch_enabled_with_document_pattern_on_attach():
    """attach_to_context must call Fetch.enable scoped to Document + Response."""
    session = FakeCDPSession()
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    enables = [p for (m, p) in session.sent if m == "Fetch.enable"]
    assert len(enables) == 1
    patterns = enables[0].get("patterns")
    assert patterns == [{"resourceType": "Document", "requestStage": "Response"}]


async def test_drain_calls_disable_and_detach():
    """drain() must Fetch.disable before detach and leave session detached."""
    session = FakeCDPSession()
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)
    await interceptor.drain()

    methods = [m for (m, _) in session.sent]
    assert "Fetch.disable" in methods
    # Disable must precede any final detach activity
    assert methods.index("Fetch.disable") >= methods.index("Fetch.enable")
    assert session.detached is True


async def test_drain_is_idempotent():
    session = FakeCDPSession()
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)
    await interceptor.drain()
    await interceptor.drain()  # must not raise


async def test_pause_after_drain_still_releases():
    """If a paused event arrives during teardown, we must still release it.

    This exercises the critical divergence from CDPResponseTap: the fetch
    interceptor does NOT drop events when drained, because each event owns
    a paused request that would otherwise hang the navigation.
    """
    session = FakeCDPSession()
    session.set_body("late-1", b"<html>late</html>")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    # Fire an event, then drain — the handler must complete regardless.
    session.fire("Fetch.requestPaused", _paused_event(request_id="late-1"))
    await interceptor.drain()

    assert "late-1" in session.continued_ids()
    assert len(sink.responses) == 1


async def test_network_id_used_as_request_id_when_present():
    """networkId correlates with Network-domain requestId bookkeeping."""
    session = FakeCDPSession()
    session.set_body("interception-abc", b"body")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire(
        "Fetch.requestPaused",
        _paused_event(request_id="interception-abc", network_id="net-xyz"),
    )
    await interceptor.drain()

    assert sink.responses[0]["request_id"] == "net-xyz"


async def test_missing_network_id_falls_back_to_fetch_id():
    """If Chromium gives us no networkId, fall back to the Fetch interception ID."""
    session = FakeCDPSession()
    session.set_body("interception-only", b"body")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    ev = _paused_event(request_id="interception-only", network_id="")
    session.fire("Fetch.requestPaused", ev)
    await interceptor.drain()

    assert sink.responses[0]["request_id"] == "interception-only"


async def test_get_body_called_exactly_once_per_pause():
    """No retries: one getResponseBody per Fetch.requestPaused."""
    session = FakeCDPSession()
    session.set_body("interception-1", b"once")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    session.fire("Fetch.requestPaused", _paused_event())
    await interceptor.drain()

    assert session.get_body_ids() == ["interception-1"]


async def test_multiple_pauses_each_released_independently():
    """N pauses → N bodies read + N continueResponse calls."""
    session = FakeCDPSession()
    session.set_body("a", b"aaa")
    session.set_body("b", b"bbb")
    session.set_body("c", b"ccc")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    for rid in ("a", "b", "c"):
        session.fire("Fetch.requestPaused", _paused_event(request_id=rid, network_id=f"n-{rid}"))
    await interceptor.drain()

    assert sorted(session.get_body_ids()) == ["a", "b", "c"]
    assert sorted(session.continued_ids()) == ["a", "b", "c"]
    assert len(sink.responses) == 3
    assert {r["body"] for r in sink.responses} == {b"aaa", b"bbb", b"ccc"}


async def test_headers_passed_through_unfiltered():
    """Regression: the old allowlist dropped 'location' and all x-* headers."""
    session = FakeCDPSession()
    session.set_body("interception-1", b"<html/>")
    sink = FakeSink()
    interceptor = CDPFetchInterceptor(sink=sink)
    ctx = FakeContext(session)
    await interceptor.attach_to_context(ctx)

    headers = [
        {"name": "Content-Type", "value": "text/html"},
        {"name": "Location", "value": "https://target.example/"},
        {"name": "X-Amz-Cf-Id", "value": "abc123"},
        {"name": "CF-Ray", "value": "ray-789"},
        {"name": "Set-Cookie", "value": "s=1; Path=/"},
        {"name": "Content-Security-Policy", "value": "default-src 'self'"},
    ]
    session.fire("Fetch.requestPaused", _paused_event(headers=headers))
    await interceptor.drain()

    captured = sink.responses[0]["response_headers"]
    # Every header should survive, lowercased.
    assert captured["location"] == "https://target.example/"
    assert captured["x-amz-cf-id"] == "abc123"
    assert captured["cf-ray"] == "ray-789"
    assert captured["set-cookie"] == "s=1; Path=/"
    assert captured["content-security-policy"] == "default-src 'self'"
