"""Favicon hash enricher.

Fetches ``/favicon.ico`` from each unique origin in the URL list and computes:
  - **mmh3 hash** (Shodan-style: ``mmh3.hash(base64.encodebytes(body))``)
  - **MD5** of the raw bytes

Both values are stored in the result data.  The mmh3 hash is created as a
FAVICON_HASH observable (this is the value Shodan indexes, enabling cross-tool
correlation).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import (
    Observable,
    ObservableLink,
    ObservableType,
    RelationshipType,
)

logger = logging.getLogger(__name__)

_FAVICON_TIMEOUT = 10.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; detonator-enrichment/1.0)"}


class FaviconEnricher(Enricher):
    """Favicon hash enricher — fetches and fingerprints /favicon.ico."""

    @property
    def name(self) -> str:
        return "favicon"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type in ("url", "domain")

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        all_origins = _unique_origins(context.urls, context.seed_url)
        if not all_origins:
            return []

        origins = [o for o in all_origins if not self._is_host_excluded(urlparse(o).hostname or "")]
        skipped = len(all_origins) - len(origins)
        if skipped:
            logger.debug("favicon: skipped %d excluded origins", skipped)
        if not origins:
            return []

        tasks = [self._fetch(origin) for origin in origins]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[EnrichmentResult] = []
        for origin, outcome in zip(origins, raw):
            if isinstance(outcome, Exception):
                results.append(
                    EnrichmentResult(
                        enricher=self.name,
                        input_value=origin,
                        error=str(outcome),
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _fetch(self, origin: str) -> EnrichmentResult:
        try:
            import mmh3
        except ImportError:
            return EnrichmentResult(
                enricher=self.name,
                input_value=origin,
                error="mmh3 not installed — install detonator[enrichment]",
            )

        favicon_url = f"{origin}/favicon.ico"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_FAVICON_TIMEOUT
            ) as client:
                resp = await client.get(favicon_url, headers=_HEADERS)
        except Exception as exc:
            return EnrichmentResult(
                enricher=self.name,
                input_value=origin,
                error=f"HTTP fetch failed: {exc}",
            )

        if resp.status_code != 200 or not resp.content:
            return EnrichmentResult(
                enricher=self.name,
                input_value=origin,
                data={"status_code": resp.status_code, "favicon_url": favicon_url},
            )

        body = resp.content
        # Shodan-style hash: mmh3.hash(base64.encodebytes(body))
        encoded = base64.encodebytes(body)
        shodan_hash = mmh3.hash(encoded)
        md5 = hashlib.md5(body).hexdigest()

        hash_str = str(shodan_hash)
        now = datetime.now(UTC)

        domain = urlparse(origin).hostname or origin
        domain_obs_id = observable_id(ObservableType.DOMAIN, domain)
        favicon_obs_id = observable_id(ObservableType.FAVICON_HASH, hash_str)

        favicon_obs = Observable(
            id=favicon_obs_id,
            type=ObservableType.FAVICON_HASH,
            value=hash_str,
            first_seen=now,
            last_seen=now,
            metadata={"md5": md5, "origin": origin},
        )
        domain_obs = Observable(
            id=domain_obs_id,
            type=ObservableType.DOMAIN,
            value=domain,
            first_seen=now,
            last_seen=now,
        )
        link = ObservableLink(
            source_id=domain_obs_id,
            target_id=favicon_obs_id,
            relationship=RelationshipType.SERVES_FAVICON,
            first_seen=now,
            last_seen=now,
        )

        return EnrichmentResult(
            enricher=self.name,
            input_value=origin,
            data={
                "favicon_url": favicon_url,
                "status_code": resp.status_code,
                "size_bytes": len(body),
                "mmh3_hash": shodan_hash,
                "md5": md5,
            },
            observables=[domain_obs, favicon_obs],
            observable_links=[link],
        )


def _unique_origins(urls: list[str], seed_url: str) -> list[str]:
    """Return unique scheme+host origins from the URL list, seed URL first."""
    seen: set[str] = set()
    result: list[str] = []

    for url in [seed_url, *urls]:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in seen:
                seen.add(origin)
                result.append(origin)

    return result
