"""Site-resource content enricher.

Iterates over captured ``site_resource`` bodies and extracts observable
indicators with per-resource URL provenance:

  - Emails              → Observable(type=EMAIL)         text/html, text/plain
  - US phone numbers    → Observable(type=PHONE)         text/html, text/plain
  - BTC / ETH wallets   → Observable(type=CRYPTO_WALLET) all text-like types
    including text/javascript

Each observable is linked FOUND_ON the domain of the specific resource URL it
was extracted from — not the seed domain, not the final landing page.  When
domainA.com redirects to domainB.com and a phone number is in domainB.com's
HTML response body, the link target is domainB.com.

Bodies are sourced from ``har_full.har`` (Playwright attach) and
``bodies/manifest.jsonl`` (agent sidecar), merged by basename — the same
union used by ``Runner._collect_artifacts()``.  Only response bodies under the
2 MiB cap are processed.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import phonenumbers

from detonator.analysis.har_body_map import load_capture_manifest, map_body_files
from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import Observable, ObservableLink, ObservableType, RelationshipType

logger = logging.getLogger(__name__)

_MAX_BODY = 2 * 1024 * 1024  # 2 MiB

# MIME types for which all extractors (email, phone, wallet) run.
_FULL_EXTRACT_MIMES = frozenset({
    "text/html",
    "text/plain",
})

# MIME types for which only the wallet extractor runs (too noisy for phone/email).
_WALLET_ONLY_MIMES = frozenset({
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/x-javascript",
})

_ALL_EXTRACT_MIMES = _FULL_EXTRACT_MIMES | _WALLET_ONLY_MIMES

# ── Regex patterns ─────────────────────────────────────────────────

_RE_EMAIL = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")

_RE_PHONE = re.compile(
    r"(?<!\d)"
    r"(\+?1[-.\s]?)?"
    r"(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})"
    r"(?!\d)",
)

_RE_BTC_LEGACY = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
_RE_BTC_BECH32 = re.compile(r"\bbc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{6,87}\b", re.IGNORECASE)
_RE_ETH = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


def _normalize_mime(raw: str) -> str:
    return (raw or "").split(";")[0].strip().lower()


class ResourceContentEnricher(Enricher):
    """Extracts indicators from captured site-resource bodies with URL provenance."""

    @property
    def name(self) -> str:
        return "resources"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "site_resources"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        artifact_dir = Path(context.artifact_dir)
        bodies_dir = artifact_dir / "bodies"
        har_path = artifact_dir / "har_full.har"

        if not bodies_dir.is_dir():
            logger.debug("run=%s bodies/ not found, skipping resource extraction", context.run_id)
            return []

        # Merge both capture paths — same union logic as Runner._collect_artifacts().
        body_map = map_body_files(har_path) if har_path.exists() else {}
        sidecar = load_capture_manifest(artifact_dir)
        for basename, ref in sidecar.items():
            body_map.setdefault(basename, ref)

        results: list[EnrichmentResult] = []
        for basename, ref in body_map.items():
            if ref.direction != "response":
                continue
            mime = _normalize_mime(ref.mime_type or "")
            if mime not in _ALL_EXTRACT_MIMES:
                continue

            body_path = bodies_dir / basename
            if not body_path.exists():
                continue
            size = body_path.stat().st_size
            if size > _MAX_BODY:
                logger.debug("run=%s skipping %s (%d bytes > cap)", context.run_id, basename, size)
                continue

            try:
                body = body_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                results.append(EnrichmentResult(
                    enricher=self.name,
                    input_value=ref.url,
                    error=f"Could not read {basename}: {exc}",
                ))
                continue

            result = _extract_from_body(
                body=body,
                source_url=ref.url,
                mime=mime,
                enricher_name=self.name,
            )
            if result.observables or result.error:
                results.append(result)

        logger.info(
            "run=%s resources: %d bodies scanned, %d results with observables",
            context.run_id, len(body_map), len(results),
        )
        return results


def _extract_from_body(body: str, source_url: str, mime: str, enricher_name: str) -> EnrichmentResult:
    now = datetime.now(UTC)
    observables: list[Observable] = []
    links: list[ObservableLink] = []
    counts: dict[str, int] = {}

    host = urlparse(source_url).hostname or ""
    host_obs_id = observable_id(ObservableType.DOMAIN, host) if host else None

    def _add(obs_type: ObservableType, value: str) -> None:
        value = value.strip()
        if not value:
            return
        obs_id = observable_id(obs_type, value)
        observables.append(Observable(
            id=obs_id,
            type=obs_type,
            value=value,
            first_seen=now,
            last_seen=now,
        ))
        counts[obs_type] = counts.get(obs_type, 0) + 1
        if host_obs_id is not None:
            links.append(ObservableLink(
                source_id=obs_id,
                target_id=host_obs_id,
                relationship=RelationshipType.FOUND_ON,
                first_seen=now,
                last_seen=now,
                evidence={"url": source_url},
            ))

    full_extract = mime in _FULL_EXTRACT_MIMES

    if full_extract:
        for m in _RE_EMAIL.finditer(body):
            _add(ObservableType.EMAIL, m.group(0).lower())

        seen_phones: set[str] = set()
        for m in _RE_PHONE.finditer(body):
            raw = m.group(0).strip()
            try:
                parsed = phonenumbers.parse(raw, "US")
            except phonenumbers.NumberParseException:
                continue
            if not phonenumbers.is_valid_number(parsed):
                continue
            e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            if e164 not in seen_phones:
                seen_phones.add(e164)
                _add(ObservableType.PHONE, e164)

    for m in _RE_BTC_LEGACY.finditer(body):
        _add(ObservableType.CRYPTO_WALLET, f"btc:{m.group(0)}")
    for m in _RE_BTC_BECH32.finditer(body):
        _add(ObservableType.CRYPTO_WALLET, f"btc:{m.group(0).lower()}")
    for m in _RE_ETH.finditer(body):
        _add(ObservableType.CRYPTO_WALLET, f"eth:{m.group(0).lower()}")

    # Ensure the host domain observable is present so FOUND_ON link targets resolve.
    if host and host_obs_id is not None:
        observables.insert(0, Observable(
            id=host_obs_id,
            type=ObservableType.DOMAIN,
            value=host,
            first_seen=now,
            last_seen=now,
        ))

    # Deduplicate observables preserving order.
    seen_ids: set[str] = set()
    unique_obs: list[Observable] = []
    for obs in observables:
        key = str(obs.id)
        if key not in seen_ids:
            seen_ids.add(key)
            unique_obs.append(obs)

    # Deduplicate links by (source, target, relationship).
    seen_links: set[str] = set()
    unique_links: list[ObservableLink] = []
    for lnk in links:
        key = f"{lnk.source_id}:{lnk.target_id}:{lnk.relationship}"
        if key not in seen_links:
            seen_links.add(key)
            unique_links.append(lnk)

    return EnrichmentResult(
        enricher=enricher_name,
        input_value=source_url,
        data={"counts": {str(k): v for k, v in counts.items()}, "mime": mime},
        observables=unique_obs,
        observable_links=unique_links,
    )
