"""Unit tests for agent.browser.network_capture.NetworkCapture.

Covers the request-capture path (via context.on("request")) and the
record_response / record_failure sink interface used by CDPResponseTap.
The old response-event path (context.on("response")) has been removed.
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
        self._handlers: dict[str, list] = {"request": []}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        try:
            self._handlers[event].remove(handler)
        except (KeyError, ValueError):
            pass

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


# ── Sink interface (record_response / record_failure) ─────────────────────


async def test_record_response_writes_body_and_manifest(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    ctx = StubContext()
    cap.attach(ctx)

    body = b"<html>hello</html>"
    await cap.record_response(
        request_id="r1",
        url="https://example.com/",
        method="GET",
        status=200,
        mime_type="text/html",
        resource_type="document",
        frame_url=None,
        remote_address="1.2.3.4:443",
        response_headers={"content-type": "text/html", "set_cookie_present": False},
        body=body,
        outcome="ok",
    )
    await cap.drain()
    stats = cap.finalize()

    sha = _sha256(body)
    assert (bodies_dir / f"{sha}.html").read_bytes() == body

    manifest = _read_manifest(bodies_dir)
    assert len(manifest) == 1
    assert manifest[0]["capture_outcome"] == "ok"
    assert manifest[0]["direction"] == "response"
    assert manifest[0]["url"] == "https://example.com/"
    assert manifest[0]["request_id"] == "r1"
    assert manifest[0]["remote_address"] == "1.2.3.4:443"
    assert stats.captured == 1
    assert stats.error == 0


async def test_record_response_none_body_empty_outcome(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    cap.attach(StubContext())

    await cap.record_response(
        request_id="r2", url="https://example.com/empty", method="GET",
        status=204, mime_type=None, resource_type=None, frame_url=None,
        remote_address=None, response_headers=None, body=None, outcome="ok",
    )
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "empty"
    assert stats.empty == 1


async def test_record_response_redirect_outcome(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    cap.attach(StubContext())

    await cap.record_response(
        request_id="r3", url="https://example.com/", method="GET",
        status=302, mime_type=None, resource_type=None, frame_url=None,
        remote_address=None, response_headers=None, body=None, outcome="redirect",
    )
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "redirect"
    assert stats.redirect == 1


async def test_record_response_truncates_at_cap(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir, max_body_bytes=10)
    cap.attach(StubContext())

    body = b"A" * 100
    await cap.record_response(
        request_id="r4", url="https://example.com/big", method="GET",
        status=200, mime_type="text/plain", resource_type=None, frame_url=None,
        remote_address=None, response_headers=None, body=body, outcome="ok",
    )
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "truncated"
    assert manifest[0]["size_actual"] == 100
    assert manifest[0]["size_truncated"] == 10
    assert stats.truncated == 1
    assert stats.captured == 1


async def test_record_response_deduplicates(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    cap.attach(StubContext())

    body = b"same content"
    for url in ("https://a.example/", "https://b.example/"):
        await cap.record_response(
            request_id=url, url=url, method="GET",
            status=200, mime_type="text/plain", resource_type=None, frame_url=None,
            remote_address=None, response_headers=None, body=body, outcome="ok",
        )
    await cap.drain()

    sha = _sha256(body)
    files = list(bodies_dir.glob(f"{sha}.*"))
    assert len(files) == 1

    manifest = _read_manifest(bodies_dir)
    assert len(manifest) == 2


async def test_record_failure_writes_manifest_entry(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    cap.attach(StubContext())

    await cap.record_failure(
        request_id="r5", url="https://example.com/gone", method="GET",
        outcome="failed", reason="net::ERR_NAME_NOT_RESOLVED",
    )
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "failed"
    assert manifest[0]["failure_reason"] == "net::ERR_NAME_NOT_RESOLVED"
    assert stats.failed == 1


async def test_record_failure_aborted(tmp_path):
    bodies_dir = tmp_path / "bodies"
    cap = NetworkCapture(bodies_dir)
    cap.attach(StubContext())

    await cap.record_failure(
        request_id="r6", url="https://example.com/", method="GET",
        outcome="aborted",
    )
    await cap.drain()
    stats = cap.finalize()

    manifest = _read_manifest(bodies_dir)
    assert manifest[0]["capture_outcome"] == "aborted"
    assert stats.aborted == 1


# ── Request path (context.on("request")) ──────────────────────────────────


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


# ── Drain lifecycle ────────────────────────────────────────────────────────


async def test_drain_detaches_request_handler(tmp_path):
    """After drain(), request events fired at the context are no longer captured."""
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    # Fire a POST before drain — should be captured
    ctx.fire_request(StubRequest(url="https://example.com/before", method="POST", post_data=b"before"))
    await cap.drain()

    # Fire after drain — must not be captured
    ctx.fire_request(StubRequest(url="https://example.com/after", method="POST", post_data=b"after_extra"))
    await asyncio.sleep(0.05)

    manifest = _read_manifest(bodies_dir)
    urls = [e.get("url") for e in manifest]
    assert "https://example.com/after" not in urls


async def test_drain_is_idempotent(tmp_path):
    """Calling drain() twice does not raise."""
    bodies_dir = tmp_path / "bodies"
    ctx = StubContext()
    cap = NetworkCapture(bodies_dir)
    cap.attach(ctx)

    ctx.fire_request(StubRequest(url="https://example.com/", method="POST", post_data=b"data"))
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
    s = CaptureStats(captured=5, truncated=1, empty=2, redirect=3, aborted=1, disposed=0, failed=2, error=0)
    d = s.as_dict()
    assert d["captured"] == 5
    assert d["truncated"] == 1
    assert d["empty"] == 2
    assert d["redirect"] == 3
    assert d["failed"] == 2
    assert set(d.keys()) == {"captured", "truncated", "empty", "redirect", "aborted", "disposed", "failed", "error"}


def test_capture_stats_bump_truncated_counts_as_captured():
    s = CaptureStats()
    s._bump("truncated")
    assert s.captured == 1
    assert s.truncated == 1
