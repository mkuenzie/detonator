"""DNS enricher — resolves A/AAAA/CNAME/MX/NS/TXT records for discovered domains.

IP addresses resolved from A/AAAA records are stored as IP observables and
linked to their parent domain with a ``resolves_to`` relationship.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import dns.asyncresolver
import dns.exception

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

_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT")


class DnsEnricher(Enricher):
    """DNS enricher backed by dnspython's async resolver."""

    @property
    def name(self) -> str:
        return "dns"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "domain"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        domains = [d for d in context.domains if not self._is_host_excluded(d)]
        skipped = len(context.domains) - len(domains)
        if skipped:
            logger.debug("dns: skipped %d excluded domains", skipped)
        if not domains:
            return []

        tasks = [self._resolve(domain) for domain in domains]
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

    async def _resolve(self, domain: str) -> EnrichmentResult:
        resolver = dns.asyncresolver.Resolver()
        records: dict[str, list[str]] = {}

        for rtype in _RECORD_TYPES:
            try:
                answer = await resolver.resolve(domain, rtype)
                records[rtype] = [r.to_text() for r in answer]
            except (dns.exception.DNSException, Exception):
                records[rtype] = []

        now = datetime.now(UTC)
        observables: list[Observable] = []
        observable_links: list[ObservableLink] = []

        domain_obs_id = observable_id(ObservableType.DOMAIN, domain)

        for rtype in ("A", "AAAA"):
            for ip_str in records.get(rtype, []):
                ip_clean = ip_str.strip()
                if not ip_clean:
                    continue
                ip_obs_id = observable_id(ObservableType.IP, ip_clean)
                observables.append(
                    Observable(
                        id=ip_obs_id,
                        type=ObservableType.IP,
                        value=ip_clean,
                        first_seen=now,
                        last_seen=now,
                    )
                )
                observable_links.append(
                    ObservableLink(
                        source_id=domain_obs_id,
                        target_id=ip_obs_id,
                        relationship=RelationshipType.RESOLVES_TO,
                        first_seen=now,
                        last_seen=now,
                        evidence={"record_type": rtype},
                    )
                )

        return EnrichmentResult(
            enricher=self.name,
            input_value=domain,
            data=records,
            observables=observables,
            observable_links=observable_links,
        )
