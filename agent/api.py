"""In-VM agent REST API.

This runs inside the sandbox VM and exposes endpoints for the host
orchestrator to trigger detonation and retrieve artifacts.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from enum import StrEnum
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent.browser.base import BrowserModule, DetonationRequest, DetonationResult, StealthProfile

logger = logging.getLogger(__name__)

app = FastAPI(title="Detonator Agent", version="0.1.0")


class AgentState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    ERROR = "error"


class DetonateBody(BaseModel):
    url: str
    timeout_sec: int = 60
    wait_for_idle: bool = True
    interactive: bool = False
    stealth: StealthProfile | None = None


class StatusResponse(BaseModel):
    state: AgentState
    error: str | None = None


class _AgentRuntime:
    """Singleton managing the current browser session and artifact state."""

    def __init__(self) -> None:
        self.browser: BrowserModule | None = None
        self.state: AgentState = AgentState.IDLE
        self.artifact_dir: Path | None = None
        self.result: DetonationResult | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    def set_browser(self, browser: BrowserModule) -> None:
        self.browser = browser

    async def run_detonation(self, request: DetonationRequest) -> None:
        assert self.browser is not None
        self.state = AgentState.RUNNING
        self.error = None
        self.result = None
        self.artifact_dir = Path(tempfile.mkdtemp(prefix="detonator_"))

        async def _pause_monitor() -> None:
            """Reflect browser pause/resume into agent state so the host can poll it."""
            while True:
                await asyncio.sleep(0.5)
                if self.browser and self.browser.is_paused:
                    self.state = AgentState.PAUSED
                elif self.state == AgentState.PAUSED:
                    self.state = AgentState.RUNNING

        try:
            await self.browser.launch(self.artifact_dir)
            monitor = (
                asyncio.create_task(_pause_monitor()) if request.interactive else None
            )
            try:
                self.result = await self.browser.detonate(request)
            finally:
                if monitor:
                    monitor.cancel()
                    try:
                        await monitor
                    except asyncio.CancelledError:
                        pass
            if self.result.error:
                self.state = AgentState.ERROR
                self.error = self.result.error
            else:
                self.state = AgentState.COMPLETE
        except Exception as exc:
            self.state = AgentState.ERROR
            self.error = str(exc)
            logger.exception("Detonation failed")
        finally:
            await self.browser.close()


runtime = _AgentRuntime()


def configure_agent(browser: BrowserModule) -> None:
    """Wire the browser module into the agent runtime. Call before starting."""
    runtime.set_browser(browser)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "browser": runtime.browser.name if runtime.browser else None}


@app.post("/detonate")
async def detonate(body: DetonateBody) -> StatusResponse:
    if runtime.state == AgentState.RUNNING:
        raise HTTPException(409, "Detonation already in progress")
    if runtime.browser is None:
        raise HTTPException(503, "No browser module configured")

    request = DetonationRequest(
        url=body.url,
        timeout_sec=body.timeout_sec,
        wait_for_idle=body.wait_for_idle,
        interactive=body.interactive,
        stealth=body.stealth,
    )
    runtime._task = asyncio.create_task(runtime.run_detonation(request))
    return StatusResponse(state=AgentState.RUNNING)


@app.get("/status")
async def status() -> StatusResponse:
    return StatusResponse(state=runtime.state, error=runtime.error)


@app.post("/resume")
async def resume() -> StatusResponse:
    if runtime.browser is None:
        raise HTTPException(503, "No browser module configured")
    await runtime.browser.resume()
    runtime.state = AgentState.RUNNING
    return StatusResponse(state=runtime.state)


_ARTIFACT_SKIP_DIRS = frozenset({"user-data"})


@app.get("/artifacts")
async def list_artifacts() -> dict:
    if runtime.artifact_dir is None or not runtime.artifact_dir.exists():
        return {"artifacts": []}
    files = [
        f.relative_to(runtime.artifact_dir).as_posix()
        for f in runtime.artifact_dir.rglob("*")
        if f.is_file()
        and not _ARTIFACT_SKIP_DIRS.intersection(f.relative_to(runtime.artifact_dir).parts)
    ]
    return {"artifacts": files}


@app.get("/artifacts/{artifact_name:path}")
async def get_artifact(artifact_name: str) -> FileResponse:
    if runtime.artifact_dir is None:
        raise HTTPException(404, "No artifacts available")
    path = (runtime.artifact_dir / artifact_name).resolve()
    if not path.is_relative_to(runtime.artifact_dir.resolve()) or not path.exists():
        raise HTTPException(404, f"Artifact not found: {artifact_name}")
    return FileResponse(path)
