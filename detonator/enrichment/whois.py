"""WHOIS enricher — queries WHOIS data for domains via asyncwhois.

Returns registration metadata (registrar, creation/expiry dates, name servers)
and creates a REGISTRANT observable when org information is present.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import Observable, ObservableType

logger = logging.getLogger(__name__)


class WhoisEnricher(Enricher):
    """WHOIS enricher backed by asyncwhois."""

    @property
    def name(self) -> str:
        return "whois"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "domain"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        domains = [d for d in context.domains if not self._is_host_excluded(d)]
        skipped = len(context.domains) - len(domains)
        if skipped:
            logger.debug("whois: skipped %d excluded domains", skipped)
        if not domains:
            return []

        try:
            import asyncwhois  # noqa: F401
        except ImportError:
            return [
                EnrichmentResult(
                    enricher=self.name,
                    input_value="",
                    error="asyncwhois not installed — install detonator[enrichment]",
                )
            ]

        tasks = [self._lookup(domain) for domain in domains]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[EnrichmentResult] = []
        for domain, outcome in zip(domains, raw):
            if isinstance(outcome, Exception):
                results.append(
                    EnrichmentResult(
                        enricher=self.name,
                        input_value=domain,
                        error=str(outcome),
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _lookup(self, domain: str) -> EnrichmentResult:
        import asyncwhois

        try:
            _text, parsed = await asyncwhois.aio_whois(domain)
        except Exception as exc:
            return EnrichmentResult(
                enricher=self.name,
                input_value=domain,
                error=f"WHOIS lookup failed: {exc}",
            )

        def _str(v: object) -> str:
            return str(v) if v is not None else ""

        def _strlist(v: object) -> list[str]:
            if v is None:
                return []
            if isinstance(v, list):
                return [str(x) for x in v]
            return [str(v)]

        data: dict = {
            "registrar": _str(parsed.get("registrar")),
            "creation_date": _str(parsed.get("creation_date")),
            "expiration_date": _str(parsed.get("expiration_date")),
            "updated_date": _str(parsed.get("updated_date")),
            "name_servers": _strlist(parsed.get("name_servers")),
            "status": _strlist(parsed.get("status")),
            "registrant_org": _str(
                parsed.get("registrant_organization") or parsed.get("org")
            ),
        }

        observables: list[Observable] = []
        org = data["registrant_org"].strip()
        if org:
            obs_id = observable_id(ObservableType.REGISTRANT, org)
            now = datetime.now(UTC)
            observables.append(
                Observable(
                    id=obs_id,
                    type=ObservableType.REGISTRANT,
                    value=org,
                    first_seen=now,
                    last_seen=now,
                )
            )

        return EnrichmentResult(
            enricher=self.name,
            input_value=domain,
            data=data,
            observables=observables,
        )
