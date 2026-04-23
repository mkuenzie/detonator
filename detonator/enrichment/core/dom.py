"""DOM content extractor.

Reads the captured ``dom.html`` artifact and extracts observable indicators
using regex and stdlib HTML parsing:

  - Emails              → Observable(type=EMAIL)
  - US-format phone numbers → Observable(type=PHONE)
  - Bitcoin (Legacy + Bech32) and Ethereum wallet addresses
                        → Observable(type=CRYPTO_WALLET)
  - ``<form action="...">`` targets → Observable(type=URL)
  - ``<meta http-equiv="refresh" content="...url=...">`` redirect targets
                        → Observable(type=URL)

All pattern matching is case-insensitive where applicable.  False-positive rate
is intentionally traded for recall at this stage — analyst review is expected.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from urllib.parse import urlparse

from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import Observable, ObservableLink, ObservableType, RelationshipType

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────

_RE_EMAIL = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
)

_RE_PHONE = re.compile(
    r"(?<!\d)"
    r"(\+?1[-.\s]?)?"
    r"(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})"
    r"(?!\d)",
)

# Bitcoin legacy (P2PKH / P2SH) — 25–34 base58 chars starting with 1 or 3
_RE_BTC_LEGACY = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")

# Bitcoin bech32 / segwit (bc1...)
_RE_BTC_BECH32 = re.compile(r"\bbc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{6,87}\b", re.IGNORECASE)

# Ethereum / EVM — 0x followed by 40 hex chars
_RE_ETH = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Meta-refresh URL extraction
_RE_META_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\"\s;>]+)", re.IGNORECASE)


class DomExtractor(Enricher):
    """DOM-content extractor — no network I/O, reads dom.html from artifact_dir."""

    @property
    def name(self) -> str:
        return "dom"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "dom"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        dom_path = Path(context.artifact_dir) / "dom.html"
        if not dom_path.exists():
            logger.debug("run=%s dom.html not found, skipping DOM extraction", context.run_id)
            return []

        try:
            html = dom_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [
                EnrichmentResult(
                    enricher=self.name,
                    input_value=str(dom_path),
                    error=f"Could not read dom.html: {exc}",
                )
            ]

        return [self._extract(html, str(dom_path), seed_url=context.seed_url)]

    def _extract(self, html: str, source: str, seed_url: str = "") -> EnrichmentResult:
        now = datetime.now(UTC)
        observables: list[Observable] = []
        links: list[ObservableLink] = []
        counts: dict[str, int] = {}

        seed_domain = urlparse(seed_url).hostname or "" if seed_url else ""
        seed_obs_id = observable_id(ObservableType.DOMAIN, seed_domain) if seed_domain else None

        _found_on_types = {ObservableType.PHONE, ObservableType.EMAIL, ObservableType.CRYPTO_WALLET}

        def _add(obs_type: ObservableType, value: str) -> None:
            value = value.strip()
            if not value:
                return
            obs_id = observable_id(obs_type, value)
            obs = Observable(
                id=obs_id,
                type=obs_type,
                value=value,
                first_seen=now,
                last_seen=now,
            )
            observables.append(obs)
            counts[obs_type] = counts.get(obs_type, 0) + 1
            if seed_obs_id is not None and obs_type in _found_on_types:
                links.append(
                    ObservableLink(
                        source_id=obs_id,
                        target_id=seed_obs_id,
                        relationship=RelationshipType.FOUND_ON,
                        first_seen=now,
                        last_seen=now,
                        evidence={"artifact": "dom.html"},
                    )
                )

        # Emails
        for m in _RE_EMAIL.finditer(html):
            _add(ObservableType.EMAIL, m.group(0).lower())

        # Phone numbers — take group 2 (the digit sequence, stripped of country code)
        seen_phones: set[str] = set()
        for m in _RE_PHONE.finditer(html):
            phone = re.sub(r"[\s\-().+]", "", m.group(0))
            if phone not in seen_phones:
                seen_phones.add(phone)
                _add(ObservableType.PHONE, phone)

        # Crypto wallets
        for m in _RE_BTC_LEGACY.finditer(html):
            _add(ObservableType.CRYPTO_WALLET, f"btc:{m.group(0)}")
        for m in _RE_BTC_BECH32.finditer(html):
            _add(ObservableType.CRYPTO_WALLET, f"btc:{m.group(0).lower()}")
        for m in _RE_ETH.finditer(html):
            _add(ObservableType.CRYPTO_WALLET, f"eth:{m.group(0).lower()}")

        # HTML structure: form actions + meta refresh targets
        parser = _DomParser()
        parser.feed(html)

        for action in parser.form_actions:
            _add(ObservableType.URL, action)
        for url in parser.meta_refresh_urls:
            _add(ObservableType.URL, url)

        # Ensure seed domain observable is present so found_on link targets resolve.
        if seed_domain and seed_obs_id is not None:
            seed_obs = Observable(
                id=seed_obs_id,
                type=ObservableType.DOMAIN,
                value=seed_domain,
                first_seen=now,
                last_seen=now,
            )
            observables.insert(0, seed_obs)

        # Deduplicate preserving order (earlier = higher priority)
        seen_ids: set[str] = set()
        unique_obs: list[Observable] = []
        for obs in observables:
            key = str(obs.id)
            if key not in seen_ids:
                seen_ids.add(key)
                unique_obs.append(obs)

        # Deduplicate links by (source, target, relationship)
        seen_links: set[str] = set()
        unique_links: list[ObservableLink] = []
        for lnk in links:
            key = f"{lnk.source_id}:{lnk.target_id}:{lnk.relationship}"
            if key not in seen_links:
                seen_links.add(key)
                unique_links.append(lnk)

        data = {
            "counts": {str(k): v for k, v in counts.items()},
            "form_actions": parser.form_actions,
            "meta_refresh_urls": parser.meta_refresh_urls,
        }

        return EnrichmentResult(
            enricher=self.name,
            input_value=source,
            data=data,
            observables=unique_obs,
            observable_links=unique_links,
        )


class _DomParser(HTMLParser):
    """Minimal HTML parser that collects form actions and meta-refresh URLs."""

    def __init__(self) -> None:
        super().__init__()
        self.form_actions: list[str] = []
        self.meta_refresh_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag.lower() == "form":
            action = attr_map.get("action", "").strip()
            if action and action not in self.form_actions:
                self.form_actions.append(action)

        elif tag.lower() == "meta":
            http_equiv = attr_map.get("http-equiv", "").lower()
            if http_equiv == "refresh":
                content = attr_map.get("content", "")
                m = _RE_META_REFRESH_URL.search(content)
                if m:
                    url = m.group(1).strip().rstrip(";")
                    if url and url not in self.meta_refresh_urls:
                        self.meta_refresh_urls.append(url)
