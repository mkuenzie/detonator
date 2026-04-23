"""Browser driver indirection — flip _DRIVER to switch between Patchright and Playwright.

Patchright patches the Runtime.enable CDP leak (the canonical tell that defeats
naive Playwright stealth) plus a cluster of smaller fingerprint vectors.
It is wire-compatible with Playwright's async API.

To revert to vanilla Playwright: change _DRIVER to "playwright" and reinstall
(``playwright install chrome``).  No other code changes required.
"""

from __future__ import annotations

# Flip to "playwright" to revert to vanilla upstream.
_DRIVER = "patchright"

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
else:
    from playwright.async_api import async_playwright  # noqa: F401
