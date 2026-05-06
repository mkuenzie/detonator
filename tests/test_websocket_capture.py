"""Tests for WebSocketCapture."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.browser.websocket_capture import WebSocketCapture


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _MockWebSocket:
    """Minimal mock of a Playwright WebSocket."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, *args) -> None:
        for h in self._handlers.get(event, []):
            h(*args)


class _MockPage:
    """Minimal mock of a Playwright Page."""

    def __init__(self, url: str = "https://example.com/") -> None:
        self.url = url
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, *args) -> None:
        for h in self._handlers.get(event, []):
            h(*args)


class _MockContext:
    """Minimal mock of a Playwright BrowserContext."""

    def __init__(self, pages: list[_MockPage] | None = None) -> None:
        self.pages = pages or []
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event: str, *args) -> None:
        for h in self._handlers.get(event, []):
            h(*args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _attach_and_run(capture: WebSocketCapture, context: _MockContext) -> None:
    await capture.attach_to_context(context)


def _make_text_frame(payload: str) -> dict:
    return {"payload": payload}


def _make_binary_frame(payload: bytes) -> dict:
    return {"payload": payload}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_frame_stored_inline(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage("https://example.com/")
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("framereceived", _make_text_frame("hello world"))
    await capture.drain()

    data = capture.finalize()
    assert len(data) == 1
    assert data[0]["url"] == "wss://example.com/ws"
    assert data[0]["page_url"] == "https://example.com/"
    assert len(data[0]["frames"]) == 1
    frame = data[0]["frames"][0]
    assert frame["direction"] == "received"
    assert frame["type"] == "text"
    assert frame["payload"] == "hello world"
    assert frame["size"] == 11
    assert "body_sha1" not in frame


@pytest.mark.asyncio
async def test_binary_frame_written_to_bodies(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    payload = b"\x00\x01\x02\x03\xde\xad\xbe\xef"
    expected_sha1 = hashlib.sha1(payload).hexdigest()

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("framesent", _make_binary_frame(payload))
    await capture.drain()

    body_file = tmp_path / f"{expected_sha1}.bin"
    assert body_file.exists()
    assert body_file.read_bytes() == payload

    data = capture.finalize()
    frame = data[0]["frames"][0]
    assert frame["direction"] == "sent"
    assert frame["type"] == "binary"
    assert frame["body_sha1"] == expected_sha1
    assert frame["body_file"] == f"bodies/{expected_sha1}.bin"
    assert frame["size"] == len(payload)
    assert "payload" not in frame


@pytest.mark.asyncio
async def test_binary_frame_deduplication(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    payload = b"\xca\xfe\xba\xbe"
    sha1 = hashlib.sha1(payload).hexdigest()

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("framereceived", _make_binary_frame(payload))
    ws.emit("framereceived", _make_binary_frame(payload))  # same payload again
    await capture.drain()

    # File should exist exactly once
    body_file = tmp_path / f"{sha1}.bin"
    assert body_file.exists()

    data = capture.finalize()
    frames = data[0]["frames"]
    assert len(frames) == 2
    # Both frames reference the same hash
    assert frames[0]["body_sha1"] == frames[1]["body_sha1"] == sha1


@pytest.mark.asyncio
async def test_close_sets_closed_at(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("close", ws)
    await capture.drain()

    data = capture.finalize()
    assert data[0]["closed_at"] is not None
    assert data[0]["error"] is None


@pytest.mark.asyncio
async def test_error_sets_error_field(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("socketerror", "connection refused")
    await capture.drain()

    data = capture.finalize()
    assert data[0]["error"] == "connection refused"


@pytest.mark.asyncio
async def test_empty_frame_connection_still_recorded(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws = _MockWebSocket("wss://example.com/ws")
    ctx = _MockContext(pages=[page])

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws)
    ws.emit("close", ws)
    await capture.drain()

    data = capture.finalize()
    assert len(data) == 1
    assert data[0]["frames"] == []
    assert data[0]["closed_at"] is not None


@pytest.mark.asyncio
async def test_no_websockets_returns_empty(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    ctx = _MockContext()
    await capture.attach_to_context(ctx)
    await capture.drain()
    assert capture.finalize() == []


@pytest.mark.asyncio
async def test_drain_idempotent(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    ctx = _MockContext()
    await capture.attach_to_context(ctx)
    await capture.drain()
    await capture.drain()  # should not raise


@pytest.mark.asyncio
async def test_new_page_registered_after_attach(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    ctx = _MockContext(pages=[])
    await capture.attach_to_context(ctx)

    # Simulate a new page opening after attach
    new_page = _MockPage("https://example.com/new")
    ctx.emit("page", new_page)

    ws = _MockWebSocket("wss://example.com/ws2")
    new_page.emit("websocket", ws)
    ws.emit("framereceived", _make_text_frame("ping"))
    await capture.drain()

    data = capture.finalize()
    assert len(data) == 1
    assert data[0]["url"] == "wss://example.com/ws2"
    assert data[0]["page_url"] == "https://example.com/new"


@pytest.mark.asyncio
async def test_multiple_connections(tmp_path: Path) -> None:
    capture = WebSocketCapture(tmp_path)
    page = _MockPage()
    ws1 = _MockWebSocket("wss://example.com/ws1")
    ws2 = _MockWebSocket("wss://example.com/ws2")
    ctx = _MockContext(pages=[page])

    await capture.attach_to_context(ctx)
    page.emit("websocket", ws1)
    page.emit("websocket", ws2)
    ws1.emit("framereceived", _make_text_frame("from-ws1"))
    ws2.emit("framesent", _make_text_frame("from-ws2"))
    await capture.drain()

    data = capture.finalize()
    assert len(data) == 2
    urls = {d["url"] for d in data}
    assert urls == {"wss://example.com/ws1", "wss://example.com/ws2"}
