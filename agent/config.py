"""Agent-side configuration and entrypoint."""

from __future__ import annotations

import logging
import sys

import uvicorn

from agent.api import app, configure_agent
from agent.browser.playwright_chromium import PlaywrightChromiumModule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    browser = PlaywrightChromiumModule()
    configure_agent(browser)

    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
