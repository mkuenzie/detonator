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
from agent.browser.websocket_capture import WebSocketCapture

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
        self._screenshot_tasks: list[asyncio.Task] = []

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
            # Playwright's --disable-features list includes features that real Chrome
            # has enabled. We ignore the whole string and replace it with a sanitized
            # version that only disables UI/noise features, not behavioral ones.
            # NOTE: this string must match Playwright's exact default — verify against
            # chrome_cmdline.txt after Playwright upgrades.
            _PW_DISABLE_FEATURES = (
                "--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,"
                "BoundaryEventDispatchTracksNodeRemoval,DestroyProfileOnBrowserClose,"
                "DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,"
                "MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,Translate,"
                "AutoDeElevate,RenderDocument,OptimizationHints"
            )
            context_kwargs: dict[str, Any] = dict(
                channel="chrome",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "--no-sandbox",  # required in VM — sandbox needs kernel features unavailable in guests
                    # Required to prevent Chrome from self-relaunching on Windows. All
                    # three are process-level / OS-integration flags and are NOT
                    # JS-observable. --edge-skip-compat-layer-relaunch despite the name
                    # also applies to Chrome (shared Chromium compat-shim code path);
                    # without it Chrome's first process spawns a brief window, exits,
                    # and relaunches itself, leaving patchright's CDP pipe attached to
                    # the dead original process.
                    "--edge-skip-compat-layer-relaunch",
                    "--no-service-autorun",
                    "--disable-field-trial-config",
                    f"--accept-lang={stealth.locale},{stealth.locale.split('-')[0]}",
                    # Replace Playwright's --disable-features with a sanitized list.
                    # Playwright disables real-Chrome behavioral features (HttpsUpgrades,
                    # PaintHolding, ThirdPartyStoragePartitioning, RenderDocument, etc.)
                    # that Turnstile can fingerprint. We keep UI/noise suppressions and
                    # Windows process-management features that patchright needs for the
                    # CDP pipe to stay attached (AutoDeElevate — without this, Chrome can
                    # relaunch itself to drop UAC elevation, breaking the CDP connection).
                    "--disable-features=AutoDeElevate,DestroyProfileOnBrowserClose,"
                    "DialMediaRouteProvider,GlobalMediaControls,MediaRouter,Translate",
                ],
                # CRITICAL: ignore_default_args filters the *final* args list (defaults +
                # ours), not just defaults. Any flag listed here will be stripped even
                # if we explicitly pass it in `args`. So this list must contain ONLY
                # flags we want gone entirely — never flags we also pass in `args`.
                # Harmless duplication on the cmdline is acceptable; missing flags are not.
                ignore_default_args=[
                    "--enable-automation",
                    "--enable-logging",
                    # Stealth-critical removals — must NOT also appear in args:
                    "--force-color-profile=srgb",       # canvas pixel shifts
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-extensions",
                    "--metrics-recording-only",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-background-timer-throttling",
                    "--disable-breakpad",
                    "--disable-dev-shm-usage",
                    "--disable-hang-monitor",
                    "--disable-infobars",
                    "--disable-prompt-on-repost",
                    "--disable-renderer-backgrounding",
                    "--disable-search-engine-choice-screen",
                    "--export-tagged-pdf",
                    # Replace Playwright's --disable-features with our sanitized version
                    # in args. NOTE: must match Playwright's exact string — verify
                    # chrome_cmdline.txt after Playwright/Patchright upgrades.
                    _PW_DISABLE_FEATURES,
                ],
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
        ws_capture = WebSocketCapture(self._artifact_dir / "bodies")

        if stealth.enabled:
            await self._context.add_init_script(
                script=f"window.__stealthLocale__ = {json.dumps(stealth.locale)};"
            )
            await self._context.add_init_script(path=str(_STEALTH_JS))

        self._page = await self._context.new_page()

        # Capture active Chrome flags by reading the OS process cmdline. This avoids
        # touching the page (navigating to chrome://version/ destabilizes patchright)
        # and also captures everything before patchright reaches us, including driver-
        # internal flags.
        try:
            import psutil
            chrome_procs = [
                p for p in psutil.process_iter(["name", "cmdline"])
                if p.info["name"] and "chrome" in p.info["name"].lower()
                and p.info["cmdline"] and user_data_dir in " ".join(p.info["cmdline"])
            ]
            if chrome_procs:
                cmdline = " ".join(chrome_procs[0].info["cmdline"])
                logger.debug("Chrome command line: %s", cmdline)
                (self._artifact_dir / "chrome_cmdline.txt").write_text(cmdline, encoding="utf-8")
        except Exception as e:
            logger.debug("Chrome cmdline capture failed: %s", e)

        # Attach CDP tap after new_page() so context.pages already includes this
        # page. attach_to_context awaits Network.enable for every page in the list,
        # guaranteeing it completes before goto() is called below.
        await tap.attach_to_context(self._context)
        await doc_intercept.attach_to_context(self._context)
        await ws_capture.attach_to_context(self._context)
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
        self._screenshot_tasks = []

        def _on_load(_: Any) -> None:
            if self._page and self._artifact_dir:
                path = self._artifact_dir / f"screenshot_{int(time.time() * 1000)}.png"

                async def _capture() -> None:
                    try:
                        await self._page.screenshot(path=str(path), full_page=True)
                        screenshot_paths.append(path)
                    except Exception:
                        logger.debug("Load-event screenshot failed (page may be navigating)")

                self._screenshot_tasks.append(asyncio.create_task(_capture()))

        self._page.on("load", _on_load)

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

            try:
                fp = await self._page.evaluate("""() => ({
                    webdriver: navigator.webdriver,
                    plugins: navigator.plugins.length,
                    plugin_names: [...navigator.plugins].map(p => p.name),
                    languages: [...navigator.languages],
                    hardware_concurrency: navigator.hardwareConcurrency,
                    max_touch_points: navigator.maxTouchPoints,
                    platform: navigator.platform,
                })""")
                logger.debug("Browser fingerprint: %s", fp)
            except Exception as fp_exc:
                logger.debug("Browser fingerprint eval failed: %s", fp_exc)

            if request.interactive:
                logger.info("Interactive mode — pausing for analyst takeover")
                self._paused.clear()
                await self._paused.wait()
                logger.info("Interactive mode — resumed")

        except Exception as exc:
            logger.error("Navigation error: %s", exc)
            await doc_intercept.drain()
            await tap.drain()
            await ws_capture.drain()
            await capture.drain()
            stats = capture.finalize()
            return DetonationResult(error=str(exc), meta=self._build_meta(stats))

        await asyncio.gather(*self._screenshot_tasks, return_exceptions=True)
        self._screenshot_tasks = []

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
        await ws_capture.drain()
        await capture.drain()
        stats = capture.finalize()

        ws_data = ws_capture.finalize()
        if ws_data:
            ws_path = self._artifact_dir / "websockets.json"
            ws_path.write_text(json.dumps(ws_data, indent=2, default=str), encoding="utf-8")

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

