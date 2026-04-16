"""TLD / domain structure enricher.

Analyses each domain's structure without external I/O:
  - TLD extraction (rightmost label)
  - Punycode / IDN detection (any label starting with ``xn--``)
  - Label count and total length
  - Subdomain depth relative to the registrable domain (labels minus 2 for
    standard two-label base like ``example.com``)

This enricher is I/O-free and runs on every domain in the context.
"""

from __future__ import annotations

import logging

from detonator.enrichment.base import Enricher, EnrichmentResult, RunContext

logger = logging.getLogger(__name__)


class TldEnricher(Enricher):
    """TLD and domain structure analyser — no network I/O."""

    @property
    def name(self) -> str:
        return "tld"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "domain"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        domains = [d for d in context.domains if not self._is_host_excluded(d)]
        skipped = len(context.domains) - len(domains)
        if skipped:
            logger.debug("tld: skipped %d excluded domains", skipped)
        return [self._analyse(domain) for domain in domains]

    def _analyse(self, domain: str) -> EnrichmentResult:
        labels = domain.lower().rstrip(".").split(".")
        tld = labels[-1] if labels else ""
        is_idn = any(label.startswith("xn--") for label in labels)

        # Decode punycoded labels for display (best-effort).
        decoded_labels: list[str] = []
        for label in labels:
            if label.startswith("xn--"):
                try:
                    decoded_labels.append(label.encode("ascii").decode("idna"))
                except (UnicodeError, UnicodeDecodeError):
                    decoded_labels.append(label)
            else:
                decoded_labels.append(label)
        decoded = ".".join(decoded_labels)

        data = {
            "tld": tld,
            "labels": labels,
            "label_count": len(labels),
            "is_idn": is_idn,
            "decoded": decoded,
            "total_length": len(domain),
            # Subdomain depth: how many labels sit above the base registrable
            # domain (approximated as last two labels).  Negative means the
            # domain has fewer than two labels.
            "subdomain_depth": max(len(labels) - 2, 0),
        }

        return EnrichmentResult(
            enricher=self.name,
            input_value=domain,
            data=data,
        )
