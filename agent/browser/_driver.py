"""Browser driver indirection — flip _DRIVER to switch between Camoufox, Patchright, and Playwright.

Camoufox (default): Firefox fork with C++-level fingerprint spoofing via
BrowserForge.  Avoids the JS-override detection vectors that defeat patched
Chromium.  Uses its own async_api — does not need async_playwright.

Patchright: patches the Runtime.enable CDP leak and a cluster of smaller
fingerprint vectors on Chromium.  Wire-compatible with Playwright's async API.

Playwright (vanilla): upstream Playwright with no stealth patches.  Useful
for debugging HAR issues where stealth overhead is a confound.

To switch drivers: change _DRIVER below and reinstall the matching binary.
  camoufox   → ``pip install camoufox[geoip] browserforge && python -m camoufox fetch``
  patchright → ``pip install patchright && patchright install chrome``
  playwright → ``playwright install chromium``
"""

from __future__ import annotations

# Change this to "patchright" or "playwright" to revert to a Chromium driver.
_DRIVER = "camoufox"

if _DRIVER == "patchright":
    try:
        from patchright.async_api import async_playwright  # noqa: F401
    except ImportError:
        import warnings
        warnings.warn(
            "patchright is not installed; falling back to vanilla playwright. "
            "Run 'pip install patchright && patchright install chrome' in the agent venv.",
            stacklevel=1,
        )
        from playwright.async_api import async_playwright  # noqa: F401
elif _DRIVER == "playwright":
    from playwright.async_api import async_playwright  # noqa: F401
else:
    # camoufox — async_playwright is not used; CamoufoxFirefoxModule imports
    # AsyncCamoufox directly.  Re-export a no-op placeholder so that any code
    # importing async_playwright from this module doesn't break at import time.
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        async_playwright = None  # type: ignore[assignment]
