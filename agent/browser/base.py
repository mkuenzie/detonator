"""Abstract base class for browser automation modules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class DetonationRequest(BaseModel):
    """Parameters for a browser detonation session."""

    url: str
    timeout_sec: int = 60
    wait_for_idle: bool = True
    interactive: bool = False
    screenshot_interval_sec: int | None = None


class DetonationResult(BaseModel):
    """Artifacts produced by a browser detonation session."""

    har_path: Path | None = None
    screenshot_paths: list[Path] = []
    dom_path: Path | None = None
    console_log_path: Path | None = None
    meta: dict[str, Any] = {}
    error: str | None = None


class BrowserModule(ABC):
    """Technology-agnostic interface for browser automation.

    Each browser engine (Playwright/Chromium, Selenium, raw CDP, etc.)
    implements this interface. The agent delegates all browser
    interaction through these methods.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier for this browser module (e.g. 'playwright_chromium')."""

    @abstractmethod
    async def launch(self, artifact_dir: Path) -> None:
        """Start the browser process.

        Args:
            artifact_dir: Directory where artifacts should be written.
        """

    @abstractmethod
    async def detonate(self, request: DetonationRequest) -> DetonationResult:
        """Navigate to the target URL and capture artifacts.

        The browser must already be launched via launch().
        """

    @abstractmethod
    async def pause(self) -> None:
        """Pause automation for interactive takeover.

        The browser stays open for manual interaction via VNC/SPICE.
        """

    @abstractmethod
    async def resume(self) -> None:
        """Resume automation after interactive pause."""

    @abstractmethod
    async def close(self) -> None:
        """Shut down the browser and release resources."""

    @property
    def is_paused(self) -> bool:
        """True while the browser is holding for analyst takeover."""
        return False
