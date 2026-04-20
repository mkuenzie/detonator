"""Playwright + Chromium implementation of the BrowserModule interface."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from agent.browser.base import BrowserModule, DetonationRequest, DetonationResult

logger = logging.getLogger(__name__)


class PlaywrightChromiumModule(BrowserModule):
    """Browser automation via Playwright driving a headed Chromium instance."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._artifact_dir: Path | None = None
        self._console_messages: list[dict] = []
        self._paused: asyncio.Event = asyncio.Event()
        self._paused.set()  # not paused initially

    @property
    def name(self) -> str:
        return "playwright_chromium"

    async def launch(self, artifact_dir: Path) -> None:
        from playwright.async_api import async_playwright

        self._artifact_dir = artifact_dir
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("Chromium launched (headed)")

    async def detonate(self, request: DetonationRequest) -> DetonationResult:
        assert self._browser is not None, "call launch() first"
        assert self._artifact_dir is not None

        har_path = self._artifact_dir / "har_full.har"
        self._console_messages = []

        self._context = await self._browser.new_context(
            record_har_path=str(har_path),
            record_har_content="attach",
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()

        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)

        screenshot_paths: list[Path] = []
        screenshot_task = None

        if request.screenshot_interval_sec:
            screenshot_task = asyncio.create_task(
                self._periodic_screenshots(request.screenshot_interval_sec, screenshot_paths)
            )

        try:
            logger.info("Navigating to %s", request.url)
            await self._page.goto(
                request.url,
                timeout=request.timeout_sec * 1000,
                wait_until="domcontentloaded",
            )

            if request.wait_for_idle:
                try:
                    await self._page.wait_for_load_state(
                        "networkidle", timeout=request.timeout_sec * 1000
                    )
                except Exception:
                    logger.warning("Network idle timeout — proceeding with capture")

            if request.interactive:
                logger.info("Interactive mode — pausing for analyst takeover")
                self._paused.clear()
                await self._paused.wait()
                logger.info("Interactive mode — resumed")

        except Exception as exc:
            logger.error("Navigation error: %s", exc)
            return DetonationResult(error=str(exc), meta=self._build_meta())

        finally:
            if screenshot_task:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass

        final_screenshot = self._artifact_dir / f"screenshot_{int(time.time())}.png"
        await self._page.screenshot(path=str(final_screenshot), full_page=True)
        screenshot_paths.append(final_screenshot)

        dom_path = self._artifact_dir / "dom.html"
        dom_content = await self._page.evaluate("document.documentElement.outerHTML")
        dom_path.write_text(dom_content, encoding="utf-8")

        console_path = self._artifact_dir / "console.json"
        console_path.write_text(
            json.dumps(self._console_messages, indent=2), encoding="utf-8"
        )

        await self._context.close()
        self._context = None
        self._page = None

        return DetonationResult(
            har_path=har_path,
            screenshot_paths=screenshot_paths,
            dom_path=dom_path,
            console_log_path=console_path,
            meta=self._build_meta(),
        )

    @property
    def is_paused(self) -> bool:
        return not self._paused.is_set()

    async def pause(self) -> None:
        self._paused.clear()

    async def resume(self) -> None:
        self._paused.set()

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
        logger.info("Chromium closed")

    def _on_console(self, msg: Any) -> None:
        self._console_messages.append({
            "type": msg.type,
            "text": msg.text,
            "timestamp": time.time(),
        })

    def _on_page_error(self, error: Any) -> None:
        self._console_messages.append({
            "type": "error",
            "text": str(error),
            "timestamp": time.time(),
        })

    def _build_meta(self) -> dict[str, Any]:
        return {
            "browser_module": self.name,
            "browser": "chromium",
        }

    async def _periodic_screenshots(
        self, interval_sec: int, paths: list[Path]
    ) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            if self._page and self._artifact_dir:
                path = self._artifact_dir / f"screenshot_{int(time.time())}.png"
                try:
                    await self._page.screenshot(path=str(path))
                    paths.append(path)
                except Exception:
                    logger.debug("Periodic screenshot failed (page may be navigating)")
