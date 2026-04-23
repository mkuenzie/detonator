"""Unit tests for agent.browser.network_capture.NetworkCapture.

Uses stub event sources and response/request objects to test the capture
logic without a real browser or Playwright installation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent.browser.network_capture import CaptureStats, NetworkCapture


# ── Stubs ──────────────────────────────────────────────────────────────────


class StubContext:
    """Minimal browser context stub that stores event handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {"response": [], "request": []}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        try:
            self._handlers[event].remove(handler)
        except (KeyError, ValueError):
            pass

    def fire_response(self, response) -> None:
        for h in list(self._handlers["response"]):
            h(response)

    def fire_request(self, request) -> None:
        for h in list(self._handlers["request"]):
            h(request)


class StubRequest:
    def __init__(
        self,
        url: str = "https://example.com/",
        method: str = "GET",
        resource_type: str = "document",
        post_data: bytes | None = None,
        headers: dict | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.post_data_buffer = post_data
        self.headers = headers or {}
        self.frame = None


class StubResponse:
    def __init__(
        self,
        url: str = "https://example.com/",
        method: str = "GET",
        status: int = 200,
        body: bytes = b"<html></html>",
        mime_type: str = "text/html",
        headers: dict | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": mime_type, **(headers or {})}
        self.request = StubRequest(url=url, method=method)
        self._body = body
        self.frame = None

    async def body(self) -> bytes:
        return self._body

    async def server_addr(self) -> dict | None:
        return None


class ErrorResponse(StubResponse):
    """Response whose body() raises an exception."""

    def __init__(self, exc: Exception, **kwargs) -> None:
        super().__init__(**kwargs)
        self._exc = exc

    async def body(self) -> bytes:
        raise self._exc


# ── Helpers ────────────────────────────────────────────────────────────────


def _read_manifest(bodies_dir: Path) -> list[dict]:
    path = bodies_dir / "manifest.jsonl"
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            lines.append(json.loads(line))
    return lines


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Basic capture ──────────────────────────────────────────────────────────


async def test_captures_response_body(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    body = b"<html>hello</html>"
    ctx.fire_response(StubResponse(body=body, mime_type="text/html"))
    await cap.drain()
    stats = cap.finalize()

    sha = _sha256(body)
    assert (bodies_dir / f"{sha}.html").read_bytes() == body

    manifest = _read_manifest(bodies_dir)
    assert len(manifest) == 1
    assert manifest[0]["capture_outcome"] == "ok"
    assert manifest[0]["direction"] == "response"
    assert manifest[0]["url"] == "https://example.com/"
    assert stats.captured == 1
    assert stats.error == 0


async def test_captures_request_body(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    post_data = b"user=alice&pass=s3cr3t"
    req = StubRequest(
        url="https://example.com/login",
        method="POST",
        post_data=post_data,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    ctx.fire_request(req)
    await cap.drain()

    sha = _sha256(post_data)
    assert (bodies_dir / f"{sha}.bin").read_bytes() == post_data

    manifest = _read_manifest(bodies_dir)
    req_entries = [e for e in manifest if e.get("direction") == "request"]
    assert len(req_entries) == 1
    assert req_entries[0]["capture_outcome"] == "ok"


async def test_request_without_post_data_is_skipped(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_request(StubRequest(url="https://example.com/", method="GET", post_data=None))
    await cap.drain()

    assert _read_manifest(bodies_dir) == []


# ── Outcome classification ─────────────────────────────────────────────────


async def test_redirect_classified_correctly(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(StubResponse(status=302, body=b"", mime_type="text/html"))
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "redirect"
    assert stats.redirect == 1
    assert stats.captured == 0
    assert not (bodies_dir).glob("*.html").__next__() if list(bodies_dir.glob("*.html")) else True


async def test_empty_body_classified_correctly(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(StubResponse(status=204, body=b"", mime_type=None))
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "empty"
    assert stats.empty == 1


async def test_disposed_body_classified_correctly(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(ErrorResponse(
        exc=Exception("Target page, context or browser has been closed"),
        status=200,
    ))
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "disposed"
    assert stats.disposed == 1
    assert stats.error == 0


async def test_aborted_body_classified_correctly(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(ErrorResponse(exc=Exception("net::ERR_ABORTED"), status=200))
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "aborted"
    assert stats.aborted == 1


# ── Size cap / truncation ──────────────────────────────────────────────────


async def test_body_truncated_at_cap(tmp_path):
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir, max_body_bytes=10)
    cap.attach(ctx)

    full_body = b"A" * 100
    ctx.fire_response(StubResponse(body=full_body, mime_type="text/plain"))
    await cap.drain()
    stats = cap.finalize()

    truncated = b"A" * 10
    sha = _sha256(truncated)
    assert (bodies_dir / f"{sha}.txt").read_bytes() == truncated

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "truncated"
    assert manifest[0]["size_actual"] == 100
    assert manifest[0]["size_truncated"] == 10
    assert stats.captured == 1
    assert stats.truncated == 1


# ── Deduplication ─────────────────────────────────────────────────────────


async def test_identical_bodies_write_one_file(tmp_path):
    """Two URLs serving identical content share one body file; both in manifest."""
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    body = b"same content"
    ctx.fire_response(StubResponse(url="https://cdn-a.example/lib.js", body=body, mime_type="application/javascript"))
    ctx.fire_response(StubResponse(url="https://cdn-b.example/lib.js", body=body, mime_type="application/javascript"))
    await cap.drain()

    sha = _sha256(body)
    files = list(bodies_dir.glob(f"{sha}.*"))
    assert len(files) == 1  # only one file on disk

    manifest = _read_manifest(bodies_dir)
    assert len(manifest) == 2  # both sources in manifest
    urls = {e["url"] for e in manifest}
    assert "https://cdn-a.example/lib.js" in urls
    assert "https://cdn-b.example/lib.js" in urls


# ── Concurrency cap ────────────────────────────────────────────────────────


async def test_concurrency_cap(tmp_path):
    """Peak concurrent body() calls must not exceed max_concurrent."""
    bodies_dir = tmp_path / "bodies"
    max_concurrent = 4
    concurrent_count = 0
    peak = 0
    lock = asyncio.Lock()

    class SlowResponse(StubResponse):
        async def body(self) -> bytes:
            nonlocal concurrent_count, peak
            async with lock:
                concurrent_count += 1
                if concurrent_count > peak:
                    peak = concurrent_count
            await asyncio.sleep(0.01)
            async with lock:
                concurrent_count -= 1
            return b"x" * 10

    ctx = StubContext()
    cap = NetworkCapture(bodies_dir, max_concurrent=max_concurrent)
    cap.attach(ctx)

    for i in range(50):
        ctx.fire_response(SlowResponse(url=f"https://example.com/res{i}"))
    await cap.drain()

    assert peak <= max_concurrent


# ── Drain lifecycle ────────────────────────────────────────────────────────


async def test_drain_detaches_handler(tmp_path):
    """After drain(), events fired at the context are no longer captured."""
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(StubResponse(url="https://example.com/before", body=b"before"))
    await cap.drain()

    # Fire event after drain — should not be captured
    ctx.fire_response(StubResponse(url="https://example.com/after", body=b"after_body_123"))
    await asyncio.sleep(0.05)  # give any stray task time to run

    manifest = _read_manifest(bodies_dir)
    urls = [e.get("url") for e in manifest]
    assert "https://example.com/after" not in urls


async def test_drain_is_idempotent(tmp_path):
    """Calling drain() twice does not raise."""
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_response(StubResponse(body=b"data"))
    await cap.drain()
    await cap.drain()  # second call must be a no-op, not raise


# ── JSONL manifest durability ──────────────────────────────────────────────


async def test_manifest_tolerates_partial_last_line(tmp_path):
    """A partial last line (crash scenario) doesn't prevent loading earlier lines."""
    from detonator.analysis.har_body_map import load_capture_manifest

    bodies = tmp_path / "bodies"
    bodies.mkdir()
    good_entry = {
        "basename": "abc.html",
        "url": "https://example.com/",
        "method": "GET",
        "direction": "response",
        "capture_outcome": "ok",
        "mime_type": "text/html",
        "size_actual": 100,
        "size_truncated": None,
        "captured_at": "2026-04-23T10:00:00+00:00",
    }
    (bodies / "manifest.jsonl").write_text(
        json.dumps(good_entry) + "\n{partial last line",
        encoding="utf-8",
    )
    result = load_capture_manifest(tmp_path)
    assert "abc.html" in result


# ── CaptureStats ──────────────────────────────────────────────────────────


def test_capture_stats_as_dict():
    s = CaptureStats(captured=5, truncated=1, empty=2, redirect=3, aborted=1, disposed=0, error=0)
    d = s.as_dict()
    assert d["captured"] == 5
    assert d["truncated"] == 1
    assert d["empty"] == 2
    assert d["redirect"] == 3
    assert set(d.keys()) == {"captured", "truncated", "empty", "redirect", "aborted", "disposed", "error"}


def test_capture_stats_bump_truncated_counts_as_captured():
    s = CaptureStats()
    s._bump("truncated")
    assert s.captured == 1
    assert s.truncated == 1
