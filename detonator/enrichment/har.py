"""HAR artifact extractor.

Parses a captured ``har_full.har`` file and returns the unique domains, IPs,
and URLs seen across all entries.  The pipeline calls this first to populate
``RunContext`` before fanning out to enrichers.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def extract_from_har(har_path: Path) -> tuple[list[str], list[str], list[str]]:
    """Return ``(domains, ips, urls)`` extracted from a HAR file.

    All three lists are sorted and deduplicated.  Domains and IPs are separated:
    a hostname that parses as a valid IP address goes into *ips*, not *domains*.
    """
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not parse HAR file %s: %s", har_path, exc)
        return [], [], []

    entries = data.get("log", {}).get("entries", [])
    domains: set[str] = set()
    ips: set[str] = set()
    urls: set[str] = set()

    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        if url:
            urls.add(url)
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if hostname:
                _classify_host(hostname, domains, ips)

        # Chromium sets serverIPAddress on each entry.
        raw_ip = entry.get("serverIPAddress", "").strip("[]")  # strip IPv6 brackets
        if raw_ip:
            _classify_host(raw_ip, domains, ips)

    return sorted(domains), sorted(ips), sorted(urls)


def _classify_host(hostname: str, domains: set[str], ips: set[str]) -> None:
    """Add *hostname* to either *domains* or *ips* based on whether it parses as an IP."""
    try:
        ipaddress.ip_address(hostname)
        ips.add(hostname)
    except ValueError:
        if hostname:
            domains.add(hostname)
