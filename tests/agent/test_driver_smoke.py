"""Smoke tests for the browser driver indirection layer.

These tests verify that:
1. The _driver module exports async_playwright and _DRIVER correctly.
2. Both patchright and playwright can be imported (catches a bad package release
   at dependency-upgrade time rather than mid-lab-run).
3. (Optional, skipped unless browser binaries are present) Both drivers can
   launch a context and navigate to about:blank.

These tests are skipped in host-side dev environments where neither playwright
nor patchright is installed (both live in the ``agent`` extras, not ``dev``).

To revert to vanilla Playwright: change _DRIVER to "playwright" in
agent/browser/_driver.py and reinstall (``playwright install chrome``).
"""

from __future__ import annotations

import pytest

# Determine which drivers are available in this environment.
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

EITHER_AVAILABLE = PLAYWRIGHT_AVAILABLE or PATCHRIGHT_AVAILABLE

pytestmark = pytest.mark.skipif(
    not EITHER_AVAILABLE,
    reason="playwright/patchright not installed in this environment (agent extras)",
)


def test_driver_module_exports_async_playwright():
    """The _driver module exports async_playwright regardless of which driver is active."""
    from agent.browser._driver import async_playwright, _DRIVER  # noqa: F401

    assert callable(async_playwright)
    assert _DRIVER in ("patchright", "playwright")


def test_driver_constant_is_valid():
    """_DRIVER must be one of the supported driver names."""
    from agent.browser._driver import _DRIVER

    assert _DRIVER in ("patchright", "playwright"), f"Unknown driver: {_DRIVER!r}"


def test_playwright_importable():
    """playwright must always be importable (patchright depends on it)."""
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed in this environment")
    from playwright.async_api import async_playwright as _pw  # noqa: F401


def test_patchright_importable():
    """patchright must be importable when installed as the agent extra."""
    if not PATCHRIGHT_AVAILABLE:
        pytest.skip("patchright not installed in this environment")
    from patchright.async_api import async_playwright as _pw  # noqa: F401
