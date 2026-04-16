"""Tests for the orchestrator's AgentClient against a mocked agent server."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from detonator.orchestrator.agent_manager import AgentManager


def _mock_transport(handler):
    return httpx.MockTransport(handler)


async def test_health_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "browser": "playwright_chromium"})

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        health = await manager.health()
        assert health.status == "ok"
        assert health.browser == "playwright_chromium"


async def test_wait_for_health_succeeds_after_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("not ready")
        return httpx.Response(200, json={"status": "ok"})

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        health = await manager.wait_for_health(timeout_sec=5, poll_sec=0.01)
        assert health.status == "ok"
        assert calls["n"] == 3


async def test_wait_for_health_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("never ready")

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        with pytest.raises(TimeoutError):
            await manager.wait_for_health(timeout_sec=0.05, poll_sec=0.01)


async def test_detonate_posts_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/detonate"
        import json as _json
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={"state": "running"})

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        status = await manager.detonate(
            "https://example.com", timeout_sec=42, interactive=True
        )
        assert status.state == "running"
        assert captured["url"] == "https://example.com"
        assert captured["timeout_sec"] == 42
        assert captured["interactive"] is True


async def test_wait_for_terminal_transitions_to_complete():
    states = iter(["running", "running", "complete"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": next(states)})

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        final = await manager.wait_for_terminal(timeout_sec=5, poll_sec=0.01)
        assert final.state == "complete"


async def test_wait_for_terminal_pauses_on_interactive():
    states = iter(["running", "paused"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": next(states)})

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        final = await manager.wait_for_terminal(
            timeout_sec=5, poll_sec=0.01, pause_on_interactive=True
        )
        assert final.state == "paused"


async def test_download_all_writes_files(tmp_path: Path):
    files = {
        "har_full.har": b'{"log": {}}',
        "dom.html": b"<html></html>",
        "screenshots/final.png": b"\x89PNG\r\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/artifacts":
            return httpx.Response(200, json={"artifacts": list(files.keys())})
        name = request.url.path.removeprefix("/artifacts/")
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404)

    async with AgentManager("http://vm:8000") as manager:
        manager._client = httpx.AsyncClient(
            base_url="http://vm:8000", transport=_mock_transport(handler)
        )
        results = await manager.download_all(tmp_path)

    assert len(results) == 3
    for name, content in files.items():
        assert (tmp_path / name).read_bytes() == content
