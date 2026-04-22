"""Tests for the HostingEnricher (Team Cymru IP-to-ASN)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from detonator.enrichment.base import RunContext
from detonator.enrichment.hosting import HostingEnricher, _reverse_ip
from detonator.models.observables import ObservableType, RelationshipType


def _ctx(tmp_path: Path, ips: list[str]) -> RunContext:
    return RunContext(
        run_id="hosting-test",
        artifact_dir=str(tmp_path),
        seed_url="https://example.com",
        ips=ips,
    )


def _make_txt_answer(txt: str):
    ans = MagicMock()
    ans.__str__ = lambda s: f'"{txt}"'
    return [ans]


@pytest.mark.asyncio
async def test_hosting_enricher_accepts_ip(tmp_path: Path) -> None:
    enricher = HostingEnricher()
    assert enricher.accepts("ip") is True
    assert enricher.accepts("domain") is False
    assert enricher.accepts("har") is False


@pytest.mark.asyncio
async def test_hosting_enricher_empty_ips(tmp_path: Path) -> None:
    enricher = HostingEnricher()
    results = await enricher.enrich(_ctx(tmp_path, []))
    assert results == []


@pytest.mark.asyncio
async def test_hosting_enricher_produces_observable_and_link(tmp_path: Path) -> None:
    origin_txt = "15169 | 8.8.8.0/24 | US | arin | 1992-12-01"
    asn_txt = "15169 | US | arin | 1992-12-01 | GOOGLE, US"

    def _mock_resolve(query, rdtype):
        if "origin.asn.cymru.com" in query:
            return _make_txt_answer(origin_txt)
        return _make_txt_answer(asn_txt)

    with patch("dns.resolver.resolve", side_effect=_mock_resolve):
        enricher = HostingEnricher()
        results = await enricher.enrich(_ctx(tmp_path, ["8.8.8.8"]))

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    obs_types = {o.type for o in result.observables}
    assert ObservableType.IP in obs_types
    assert ObservableType.HOSTING_PROVIDER in obs_types

    provider_obs = next(o for o in result.observables if o.type == ObservableType.HOSTING_PROVIDER)
    assert "15169" in provider_obs.value
    assert "GOOGLE" in provider_obs.value

    assert len(result.observable_links) == 1
    assert result.observable_links[0].relationship == RelationshipType.HOSTED_BY


@pytest.mark.asyncio
async def test_hosting_enricher_dns_failure_returns_error(tmp_path: Path) -> None:
    import dns.exception

    with patch("dns.resolver.resolve", side_effect=dns.exception.DNSException("NXDOMAIN")):
        enricher = HostingEnricher()
        results = await enricher.enrich(_ctx(tmp_path, ["192.0.2.1"]))

    assert len(results) == 1
    assert results[0].error is not None


@pytest.mark.asyncio
async def test_hosting_enricher_as_name_lookup_failure_still_succeeds(tmp_path: Path) -> None:
    """AS name lookup failure should not abort; result uses ASN only."""
    import dns.exception

    origin_txt = "64496 | 198.51.100.0/24 | US | arin | 2010-01-01"

    call_count = 0

    def _mock_resolve(query, rdtype):
        nonlocal call_count
        call_count += 1
        if "origin.asn.cymru.com" in query:
            return _make_txt_answer(origin_txt)
        raise dns.exception.DNSException("no AS record")

    with patch("dns.resolver.resolve", side_effect=_mock_resolve):
        enricher = HostingEnricher()
        results = await enricher.enrich(_ctx(tmp_path, ["198.51.100.1"]))

    assert results[0].error is None
    provider = next(o for o in results[0].observables if o.type == ObservableType.HOSTING_PROVIDER)
    assert "64496" in provider.value


def test_reverse_ip_v4() -> None:
    assert _reverse_ip("8.8.8.8") == "8.8.8.8"


def test_reverse_ip_v4_asymmetric() -> None:
    assert _reverse_ip("1.2.3.4") == "4.3.2.1"


def test_reverse_ip_invalid() -> None:
    assert _reverse_ip("not-an-ip") is None
