"""Playwright + Chromium implementation of the BrowserModule interface."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.browser._driver import _DRIVER, async_playwright
from agent.browser.base import BrowserModule, DetonationRequest, DetonationResult, StealthProfile
from agent.browser.cdp_response_tap import CDPResponseTap
from agent.browser.network_capture import NetworkCapture
from agent.browser.route_document_interceptor import RouteDocumentInterceptor

logger = logging.getLogger(__name__)

_STEALTH_JS = Path(__file__).parent / "stealth.js"


class PlaywrightChromiumModule(BrowserModule):
    """Browser automation via Playwright (or Patchright) driving a real Chrome install.

    Uses launch_persistent_context() so the profile looks like a real user
    session rather than a freshly-created automation context.  Stealth
    hardening (navigator.webdriver removal, plugin shimming, WebGL spoofing,
    etc.) is applied via an init script loaded from stealth.js.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._artifact_dir: Path | None = None
        self._console_messages: list[dict] = []
        self._navigations: list[dict] = []
        self._paused: asyncio.Event = asyncio.Event()
        self._paused.set()  # not paused initially
        self._stealth_enabled: bool = True

    @property
    def name(self) -> str:
        return "playwright_chromium"

    async def launch(self, artifact_dir: Path) -> None:
        """Start the driver.  The browser itself launches in detonate()."""
        self._artifact_dir = artifact_dir
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        logger.info("Driver '%s' started (browser launches at detonate time)", _DRIVER)

    async def detonate(self, request: DetonationRequest) -> DetonationResult:
        assert self._playwright is not None, "call launch() first"
        assert self._artifact_dir is not None

        har_path = self._artifact_dir / "har_full.har"
        self._console_messages = []
        self._navigations = []

        stealth = request.stealth if request.stealth is not None else StealthProfile()
        self._stealth_enabled = stealth.enabled

        user_data_dir = str(self._artifact_dir / "user-data")

        if stealth.enabled:
            context_kwargs: dict[str, Any] = dict(
                channel="chrome",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--password-store=basic",
                    "--use-mock-keychain",
                ],
                ignore_default_args=["--enable-automation", "--enable-logging"],
                locale=stealth.locale,
                timezone_id=stealth.timezone_id,
                viewport={"width": stealth.viewport_width, "height": stealth.viewport_height},
                screen={"width": stealth.viewport_width, "height": stealth.viewport_height},
                color_scheme="light",
                reduced_motion="no-preference",
                forced_colors="none",
                extra_http_headers={
                    "Accept-Language": f"{stealth.locale},{stealth.locale.split('-')[0]};q=0.9"
                },
                geolocation={
                    "latitude": stealth.geolocation_lat,
                    "longitude": stealth.geolocation_lon,
                },
                permissions=["geolocation"],
                record_har_path=str(har_path),
                record_har_content="attach",
                ignore_https_errors=True,
            )
            if stealth.user_agent:
                context_kwargs["user_agent"] = stealth.user_agent
        else:
            context_kwargs = dict(
                headless=False,
                record_har_path=str(har_path),
                record_har_content="attach",
                ignore_https_errors=True,
            )

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir, **context_kwargs
        )

        capture = NetworkCapture(self._artifact_dir / "bodies")
        capture.attach(self._context)
        tap = CDPResponseTap(sink=capture)
        doc_intercept = RouteDocumentInterceptor(sink=capture)

        if stealth.enabled:
            await self._context.add_init_script(
                script=f"window.__stealthLocale__ = {json.dumps(stealth.locale)};"
            )
            await self._context.add_init_script(path=str(_STEALTH_JS))

        self._page = await self._context.new_page()
        # Attach CDP tap after new_page() so context.pages already includes this
        # page. attach_to_context awaits Network.enable for every page in the list,
        # guaranteeing it completes before goto() is called below.
        await tap.attach_to_context(self._context)
        await doc_intercept.attach_to_context(self._context)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)

        def _on_framenavigated(frame: Any) -> None:
            self._navigations.append({
                "timestamp": datetime.now(UTC).isoformat(),
                "url": frame.url,
                "frame": "main" if frame == self._page.main_frame else "sub",
            })

        self._page.on("framenavigated", _on_framenavigated)

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
            await doc_intercept.drain()
            await tap.drain()
            await capture.drain()
            stats = capture.finalize()
            return DetonationResult(error=str(exc), meta=self._build_meta(stats))

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

        navigations_path = self._artifact_dir / "navigations.json"
        navigations_path.write_text(
            json.dumps(self._navigations, indent=2), encoding="utf-8"
        )

        console_path = self._artifact_dir / "console.json"
        console_path.write_text(
            json.dumps(self._console_messages, indent=2), encoding="utf-8"
        )
        
        await doc_intercept.drain()
        await tap.drain()
        await capture.drain()
        stats = capture.finalize()

        await self._context.close()
        self._context = None
        self._page = None

        return DetonationResult(
            har_path=har_path,
            screenshot_paths=screenshot_paths,
            dom_path=dom_path,
            navigations_path=navigations_path,
            console_log_path=console_path,
            meta=self._build_meta(stats),
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
            self._context = None
        if self._playwright:
            await self._playwright.stop()
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

    def _build_meta(self, stats: NetworkCapture | None = None) -> dict[str, Any]:
        from agent.browser.network_capture import CaptureStats
        meta: dict[str, Any] = {
            "browser_module": self.name,
            "browser": "chrome" if self._stealth_enabled else "chromium",
            "stealth_enabled": self._stealth_enabled,
            "browser_driver": _DRIVER,
        }
        if isinstance(stats, CaptureStats):
            meta["capture_stats"] = stats.as_dict()
        return meta

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
