"""Tests for DomExtractor — focusing on found_on links for phone/email/wallet."""

from __future__ import annotations

from pathlib import Path

import pytest

from detonator.enrichment.base import RunContext
from detonator.enrichment.core.dom import DomExtractor
from detonator.models.observables import ObservableType, RelationshipType


def _ctx(tmp_path: Path, seed_url: str = "https://example.com") -> RunContext:
    return RunContext(
        run_id="dom-test",
        artifact_dir=str(tmp_path),
        seed_url=seed_url,
    )


def _write_dom(tmp_path: Path, html: str) -> None:
    (tmp_path / "dom.html").write_text(html, encoding="utf-8")


@pytest.mark.asyncio
async def test_phone_produces_found_on_link(tmp_path: Path) -> None:
    _write_dom(tmp_path, "<p>Call us: (555) 867-5309</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://example.com"))

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    phone_obs = next((o for o in result.observables if o.type == ObservableType.PHONE), None)
    assert phone_obs is not None

    found_on_links = [
        lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON
    ]
    assert len(found_on_links) >= 1
    assert any(str(lk.source_id) == str(phone_obs.id) for lk in found_on_links)


@pytest.mark.asyncio
async def test_email_produces_found_on_link(tmp_path: Path) -> None:
    _write_dom(tmp_path, "<p>Contact: info@evil.com</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://phish.example.com/lure"))

    result = results[0]
    email_obs = next((o for o in result.observables if o.type == ObservableType.EMAIL), None)
    assert email_obs is not None

    found_on_links = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert any(str(lk.source_id) == str(email_obs.id) for lk in found_on_links)


@pytest.mark.asyncio
async def test_crypto_wallet_produces_found_on_link(tmp_path: Path) -> None:
    btc = "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf Na"
    _write_dom(tmp_path, f"<p>Send BTC to {btc}</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://example.com"))

    result = results[0]
    wallet_obs = next((o for o in result.observables if o.type == ObservableType.CRYPTO_WALLET), None)
    assert wallet_obs is not None

    found_on_links = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert any(str(lk.source_id) == str(wallet_obs.id) for lk in found_on_links)


@pytest.mark.asyncio
async def test_found_on_link_target_is_seed_domain(tmp_path: Path) -> None:
    _write_dom(tmp_path, "<p>Call (555) 867-5309</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://seed.example.com/path?q=1"))

    result = results[0]
    found_on_links = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert len(found_on_links) >= 1

    # All found_on links should target the seed domain observable
    from detonator.enrichment.base import observable_id
    seed_id = str(observable_id(ObservableType.DOMAIN, "seed.example.com"))
    for lk in found_on_links:
        assert str(lk.target_id) == seed_id


@pytest.mark.asyncio
async def test_found_on_evidence_includes_artifact(tmp_path: Path) -> None:
    _write_dom(tmp_path, "<p>Email: test@phish.com</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://example.com"))

    found_on_links = [lk for lk in results[0].observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert all(lk.evidence and lk.evidence.get("artifact") == "dom.html" for lk in found_on_links)


@pytest.mark.asyncio
async def test_seed_domain_observable_present_in_result(tmp_path: Path) -> None:
    _write_dom(tmp_path, "<p>Phone: (555) 123-4567</p>")
    enricher = DomExtractor()
    results = await enricher.enrich(_ctx(tmp_path, "https://landing.example.com"))

    obs_values = {o.value for o in results[0].observables}
    assert "landing.example.com" in obs_values


@pytest.mark.asyncio
async def test_no_found_on_links_without_seed_url(tmp_path: Path) -> None:
    """If seed_url yields no hostname, no found_on links are created."""
    _write_dom(tmp_path, "<p>Call (555) 000-0000</p>")
    enricher = DomExtractor()
    result = enricher._extract("<p>Call (555) 000-0000</p>", "dom.html", seed_url="")

    found_on = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert len(found_on) == 0
