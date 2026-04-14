"""HTTP manager for the in-VM agent REST API.

The orchestrator uses this to drive the agent from outside the sandbox:
poll for health, start detonation, poll status, and download artifacts.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AgentStatus(BaseModel):
    state: str
    error: str | None = None


class AgentHealth(BaseModel):
    status: str
    browser: str | None = None


class AgentManager:
    """Async client for a single in-VM agent instance."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> AgentManager:
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "use `async with AgentManager(...)`"
        return self._client

    # ── Health ────────────────────────────────────────────────────

    async def health(self) -> AgentHealth:
        resp = await self.client.get("/health")
        resp.raise_for_status()
        return AgentHealth(**resp.json())

    async def wait_for_health(
        self, *, timeout_sec: float, poll_sec: float = 2.0
    ) -> AgentHealth:
        """Poll `/health` until it succeeds or timeout expires."""
        deadline = asyncio.get_event_loop().time() + timeout_sec
        last_exc: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                return await self.health()
            except (httpx.HTTPError, httpx.ConnectError) as exc:
                last_exc = exc
                await asyncio.sleep(poll_sec)
        raise TimeoutError(
            f"Agent at {self._base_url} not healthy within {timeout_sec}s"
        ) from last_exc

    # ── Detonation ────────────────────────────────────────────────

    async def detonate(
        self,
        url: str,
        *,
        timeout_sec: int = 60,
        wait_for_idle: bool = True,
        interactive: bool = False,
        screenshot_interval_sec: int | None = None,
    ) -> AgentStatus:
        payload = {
            "url": url,
            "timeout_sec": timeout_sec,
            "wait_for_idle": wait_for_idle,
            "interactive": interactive,
            "screenshot_interval_sec": screenshot_interval_sec,
        }
        resp = await self.client.post("/detonate", json=payload)
        resp.raise_for_status()
        return AgentStatus(**resp.json())

    async def status(self) -> AgentStatus:
        resp = await self.client.get("/status")
        resp.raise_for_status()
        return AgentStatus(**resp.json())

    async def resume(self) -> AgentStatus:
        resp = await self.client.post("/resume")
        resp.raise_for_status()
        return AgentStatus(**resp.json())

    async def wait_for_terminal(
        self,
        *,
        timeout_sec: float,
        poll_sec: float = 2.0,
        pause_on_interactive: bool = False,
    ) -> AgentStatus:
        """Poll `/status` until state is terminal.

        Terminal states: `complete`, `error`. If ``pause_on_interactive`` is
        True, returns when state becomes `paused` so the orchestrator can
        hold for analyst takeover before calling :meth:`resume`.
        """
        terminal = {"complete", "error"}
        if pause_on_interactive:
            terminal = terminal | {"paused"}

        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            st = await self.status()
            if st.state in terminal:
                return st
            await asyncio.sleep(poll_sec)
        raise TimeoutError(
            f"Agent did not reach a terminal state within {timeout_sec}s"
        )

    # ── Artifacts ─────────────────────────────────────────────────

    async def list_artifacts(self) -> list[str]:
        resp = await self.client.get("/artifacts")
        resp.raise_for_status()
        return resp.json().get("artifacts", [])

    async def download_artifact(self, name: str, dest: Path) -> int:
        """Stream one artifact file to ``dest``. Returns bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        async with self.client.stream("GET", f"/artifacts/{name}") as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    total += len(chunk)
        return total

    async def download_all(self, dest_dir: Path) -> list[tuple[str, Path, int]]:
        """Download every artifact listed by the agent into ``dest_dir``.

        Returns list of (name, path, size) tuples. Artifact names may include
        sub-paths (e.g. ``screenshots/0001.png``) — those are preserved.
        """
        names = await self.list_artifacts()
        results: list[tuple[str, Path, int]] = []
        for name in names:
            dest = dest_dir / name
            size = await self.download_artifact(name, dest)
            results.append((name, dest, size))
            logger.debug("Downloaded artifact %s (%d bytes)", name, size)
        return results
