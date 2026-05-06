"""Smoke tests for the browser driver indirection layer.

These tests verify that:
1. The _driver module exports _DRIVER correctly.
2. Camoufox, patchright, and playwright can each be imported (catches a bad
   package release at dependency-upgrade time rather than mid-lab-run).
3. CamoufoxFirefoxModule and PlaywrightChromiumModule satisfy the BrowserModule ABC.

These tests are skipped in host-side dev environments where the agent extras
are not installed (camoufox/playwright/patchright live in the ``agent`` extras,
not ``dev``).

To switch drivers: change _DRIVER in agent/browser/_driver.py.
  camoufox   → default; Firefox + C++-level fingerprint spoofing
  patchright → patched Chromium fallback
  playwright → vanilla Chromium for debugging
"""

from __future__ import annotations

import pytest

try:
    import camoufox as _cf  # noqa: F401
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

try:
    import playwright as _pl  # noqa: F401
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    import patchright as _pr  # noqa: F401
    PATCHRIGHT_AVAILABLE = True
except ImportError:
    PATCHRIGHT_AVAILABLE = False

ANY_AVAILABLE = CAMOUFOX_AVAILABLE or PLAYWRIGHT_AVAILABLE or PATCHRIGHT_AVAILABLE

pytestmark = pytest.mark.skipif(
    not ANY_AVAILABLE,
    reason="agent browser extras not installed in this environment",
)


def test_driver_constant_is_valid():
    """_DRIVER must be one of the supported driver names."""
    from agent.browser._driver import _DRIVER

    assert _DRIVER in ("camoufox", "patchright", "playwright"), f"Unknown driver: {_DRIVER!r}"


def test_driver_default_is_camoufox():
    """Default driver should be camoufox."""
    from agent.browser._driver import _DRIVER

    assert _DRIVER == "camoufox", (
        f"Expected default driver 'camoufox', got {_DRIVER!r}. "
        "Update this test if intentionally switching the default."
    )


def test_camoufox_importable():
    """camoufox and browserforge must be importable."""
    if not CAMOUFOX_AVAILABLE:
        pytest.skip("camoufox not installed in this environment")
    from camoufox.async_api import AsyncCamoufox  # noqa: F401
    from browserforge.fingerprints import FingerprintGenerator  # noqa: F401


def test_playwright_importable():
    """playwright must be importable (patchright peer dep and fallback driver)."""
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed in this environment")
    from playwright.async_api import async_playwright as _pw  # noqa: F401


def test_patchright_importable():
    """patchright must be importable when installed as the agent extra."""
    if not PATCHRIGHT_AVAILABLE:
        pytest.skip("patchright not installed in this environment")
    from patchright.async_api import async_playwright as _pw  # noqa: F401


def test_camoufox_module_satisfies_abc():
    """CamoufoxFirefoxModule must be a concrete BrowserModule implementation."""
    if not CAMOUFOX_AVAILABLE:
        pytest.skip("camoufox not installed in this environment")
    from agent.browser.base import BrowserModule
    from agent.browser.camoufox_firefox import CamoufoxFirefoxModule

    assert issubclass(CamoufoxFirefoxModule, BrowserModule)
    instance = CamoufoxFirefoxModule()
    assert isinstance(instance, BrowserModule)
    assert instance.name == "camoufox_firefox"


def test_playwright_chromium_module_satisfies_abc():
    """PlaywrightChromiumModule must remain a concrete BrowserModule implementation."""
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed in this environment")
    from agent.browser.base import BrowserModule
    from agent.browser.playwright_chromium import PlaywrightChromiumModule

    assert issubclass(PlaywrightChromiumModule, BrowserModule)
    instance = PlaywrightChromiumModule()
    assert isinstance(instance, BrowserModule)
    assert instance.name == "playwright_chromium"
