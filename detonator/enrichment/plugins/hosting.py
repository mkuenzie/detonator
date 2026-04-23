"""Hosting provider enricher via Team Cymru IP-to-ASN mapping.

For each IP in the run, queries Team Cymru's DNS service:
  <reversed-octets>.origin.asn.cymru.com  TXT → ASN | BGP Prefix | CC | Registry | Alloc Date
  AS<asn>.asn.cymru.com                   TXT → ASN | CC | Registry | Alloc Date | AS Name

Creates one HOSTING_PROVIDER observable per unique ASN and links each IP to it
with a HOSTED_BY edge.  NXDOMAIN / timeout are handled gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from ipaddress import AddressValueError, IPv4Address, IPv6Address

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

_DNS_TIMEOUT = 5.0


class HostingEnricher(Enricher):
    """Resolve IP → ASN → hosting provider via Team Cymru DNS."""

    @property
    def name(self) -> str:
        return "hosting"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "ip"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        ips = context.ips
        if not ips:
            return []

        tasks = [self._probe(ip) for ip in ips]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[EnrichmentResult] = []
        for ip, outcome in zip(ips, raw):
            if isinstance(outcome, Exception):
                results.append(
                    EnrichmentResult(
                        enricher=self.name,
                        input_value=ip,
                        error=str(outcome),
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _probe(self, ip: str) -> EnrichmentResult:
        try:
            import dns.exception
            import dns.resolver
        except ImportError:
            return EnrichmentResult(
                enricher=self.name,
                input_value=ip,
                error="dnspython not installed — install detonator[enrichment]",
            )

        reversed_ip = _reverse_ip(ip)
        if reversed_ip is None:
            return EnrichmentResult(
                enricher=self.name,
                input_value=ip,
                error=f"Could not parse IP address: {ip}",
            )

        origin_query = f"{reversed_ip}.origin.asn.cymru.com"
        try:
            answers = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, lambda: dns.resolver.resolve(origin_query, "TXT")
                ),
                timeout=_DNS_TIMEOUT,
            )
        except (dns.exception.DNSException, OSError) as exc:
            return EnrichmentResult(
                enricher=self.name,
                input_value=ip,
                error=f"Cymru origin lookup failed: {exc}",
            )

        # TXT record format: "ASN | BGP Prefix | CC | Registry | Alloc Date"
        txt = str(answers[0]).strip('"').strip()
        parts = [p.strip() for p in txt.split("|")]
        if len(parts) < 2:
            return EnrichmentResult(
                enricher=self.name,
                input_value=ip,
                error=f"Unexpected Cymru origin response: {txt!r}",
            )

        asn = parts[0].strip()
        bgp_prefix = parts[1].strip() if len(parts) > 1 else ""
        country = parts[2].strip() if len(parts) > 2 else ""
        registry = parts[3].strip() if len(parts) > 3 else ""

        # Strip leading "AS" if present for the follow-up query
        asn_num = asn.removeprefix("AS").removeprefix("as")

        as_name = ""
        asn_query = f"AS{asn_num}.asn.cymru.com"
        try:
            as_answers = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, lambda: dns.resolver.resolve(asn_query, "TXT")
                ),
                timeout=_DNS_TIMEOUT,
            )
            as_txt = str(as_answers[0]).strip('"').strip()
            as_parts = [p.strip() for p in as_txt.split("|")]
            # Format: "ASN | CC | Registry | Alloc Date | AS Name"
            if len(as_parts) >= 5:
                as_name = as_parts[4].strip()
        except (dns.exception.DNSException, OSError):
            pass  # AS name is nice-to-have; don't fail the whole enrichment

        # Build HOSTING_PROVIDER observable value: "AS{asn} ({as_name})"
        if as_name:
            provider_value = f"AS{asn_num} ({as_name})"
        else:
            provider_value = f"AS{asn_num}"

        now = datetime.now(UTC)
        ip_obs_id = observable_id(ObservableType.IP, ip)
        provider_obs_id = observable_id(ObservableType.HOSTING_PROVIDER, provider_value)

        ip_obs = Observable(
            id=ip_obs_id,
            type=ObservableType.IP,
            value=ip,
            first_seen=now,
            last_seen=now,
        )
        provider_obs = Observable(
            id=provider_obs_id,
            type=ObservableType.HOSTING_PROVIDER,
            value=provider_value,
            first_seen=now,
            last_seen=now,
            metadata={
                "asn": f"AS{asn_num}",
                "as_name": as_name,
                "country": country,
                "registry": registry,
                "bgp_prefix": bgp_prefix,
            },
        )
        link = ObservableLink(
            source_id=ip_obs_id,
            target_id=provider_obs_id,
            relationship=RelationshipType.HOSTED_BY,
            first_seen=now,
            last_seen=now,
            evidence={"bgp_prefix": bgp_prefix, "source": "cymru"},
        )

        return EnrichmentResult(
            enricher=self.name,
            input_value=ip,
            data={
                "asn": f"AS{asn_num}",
                "as_name": as_name,
                "bgp_prefix": bgp_prefix,
                "country": country,
                "registry": registry,
            },
            observables=[ip_obs, provider_obs],
            observable_links=[link],
        )


def _reverse_ip(ip: str) -> str | None:
    """Return the reversed-octet form used by Cymru's DNS service."""
    try:
        addr = IPv4Address(ip)
        octets = str(addr).split(".")
        return ".".join(reversed(octets))
    except AddressValueError:
        pass
    try:
        addr6 = IPv6Address(ip)
        # Full expanded form, then reverse nibbles
        full = addr6.exploded.replace(":", "")
        return ".".join(reversed(full))
    except AddressValueError:
        return None
