"""Tests for ResourceContentEnricher — per-resource observable extraction with URL provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detonator.analysis.har_body_map import HarBodyRef
from detonator.enrichment.base import RunContext, observable_id
from detonator.enrichment.core.resources import ResourceContentEnricher, _extract_from_body
from detonator.models.observables import ObservableType, RelationshipType


def _ctx(tmp_path: Path, seed_url: str = "https://seed.example.com") -> RunContext:
    return RunContext(run_id="res-test", artifact_dir=str(tmp_path), seed_url=seed_url)


def _write_body(tmp_path: Path, basename: str, content: str) -> Path:
    bodies = tmp_path / "bodies"
    bodies.mkdir(exist_ok=True)
    p = bodies / basename
    p.write_text(content, encoding="utf-8")
    return p


def _write_manifest(tmp_path: Path, entries: list[dict]) -> None:
    bodies = tmp_path / "bodies"
    bodies.mkdir(exist_ok=True)
    lines = "\n".join(json.dumps(e) for e in entries)
    (bodies / "manifest.jsonl").write_text(lines, encoding="utf-8")


# ── _extract_from_body unit tests ──────────────────────────────────

def test_extract_email_links_to_resource_host() -> None:
    result = _extract_from_body(
        body="<p>Contact: info@evil.com</p>",
        source_url="https://landing.evil.com/page",
        mime="text/html",
        enricher_name="resources",
    )
    email_obs = next((o for o in result.observables if o.type == ObservableType.EMAIL), None)
    assert email_obs is not None
    assert email_obs.value == "info@evil.com"

    landing_id = str(observable_id(ObservableType.DOMAIN, "landing.evil.com"))
    found_on = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert len(found_on) == 1
    assert str(found_on[0].target_id) == landing_id


def test_extract_phone_links_to_resource_host() -> None:
    result = _extract_from_body(
        body="<p>Call us: (415) 867-5309</p>",
        source_url="https://domainB.com/",
        mime="text/html",
        enricher_name="resources",
    )
    phone_obs = next((o for o in result.observables if o.type == ObservableType.PHONE), None)
    assert phone_obs is not None

    domain_b_id = str(observable_id(ObservableType.DOMAIN, "domainb.com"))
    found_on = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert any(str(lk.target_id) == domain_b_id for lk in found_on)


def test_extract_btc_wallet_from_html() -> None:
    result = _extract_from_body(
        body="<p>Send to: 1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf Na</p>",
        source_url="https://scam.com/",
        mime="text/html",
        enricher_name="resources",
    )
    wallet = next((o for o in result.observables if o.type == ObservableType.CRYPTO_WALLET), None)
    assert wallet is not None
    assert wallet.value.startswith("btc:")


def test_extract_eth_wallet_from_javascript() -> None:
    result = _extract_from_body(
        body="const addr = '0xAbCd1234567890abcdef1234567890ABCDEF1234';",
        source_url="https://crypto-site.com/app.js",
        mime="text/javascript",
        enricher_name="resources",
    )
    wallet = next((o for o in result.observables if o.type == ObservableType.CRYPTO_WALLET), None)
    assert wallet is not None
    assert wallet.value == "eth:0xabcd1234567890abcdef1234567890abcdef1234"


def test_no_phone_or_email_from_javascript() -> None:
    """JS bodies only run the wallet extractor — phone/email are too noisy."""
    result = _extract_from_body(
        body="var contact = 'info@evil.com'; var phone = '(415) 867-5309';",
        source_url="https://example.com/app.js",
        mime="text/javascript",
        enricher_name="resources",
    )
    assert not any(o.type == ObservableType.EMAIL for o in result.observables)
    assert not any(o.type == ObservableType.PHONE for o in result.observables)


def test_found_on_evidence_contains_url() -> None:
    result = _extract_from_body(
        body="<p>Email: test@phish.com</p>",
        source_url="https://phish.com/lure",
        mime="text/html",
        enricher_name="resources",
    )
    found_on = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert all(lk.evidence and lk.evidence.get("url") == "https://phish.com/lure" for lk in found_on)


def test_host_domain_observable_included() -> None:
    result = _extract_from_body(
        body="<p>Call (415) 123-4567</p>",
        source_url="https://landing.example.com/page",
        mime="text/html",
        enricher_name="resources",
    )
    obs_values = {o.value for o in result.observables}
    assert "landing.example.com" in obs_values


def test_no_found_on_links_when_no_host() -> None:
    result = _extract_from_body(
        body="<p>Call (415) 000-0000</p>",
        source_url="",
        mime="text/html",
        enricher_name="resources",
    )
    found_on = [lk for lk in result.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert len(found_on) == 0


# ── ResourceContentEnricher integration tests ──────────────────────

@pytest.mark.asyncio
async def test_enricher_skips_when_no_bodies_dir(tmp_path: Path) -> None:
    enricher = ResourceContentEnricher()
    results = await enricher.enrich(_ctx(tmp_path))
    assert results == []


@pytest.mark.asyncio
async def test_enricher_reads_from_manifest(tmp_path: Path) -> None:
    basename = "abc123.html"
    _write_body(tmp_path, basename, "<p>Contact: ops@evil.com</p>")
    _write_manifest(tmp_path, [{
        "basename": basename,
        "url": "https://evil.com/page",
        "direction": "response",
        "method": "GET",
        "mime_type": "text/html",
        "capture_outcome": "ok",
    }])

    enricher = ResourceContentEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    assert len(results) == 1
    email_obs = next((o for o in results[0].observables if o.type == ObservableType.EMAIL), None)
    assert email_obs is not None
    assert email_obs.value == "ops@evil.com"

    evil_id = str(observable_id(ObservableType.DOMAIN, "evil.com"))
    found_on = [lk for lk in results[0].observable_links if lk.relationship == RelationshipType.FOUND_ON]
    assert any(str(lk.target_id) == evil_id for lk in found_on)


@pytest.mark.asyncio
async def test_enricher_skips_request_bodies(tmp_path: Path) -> None:
    basename = "post_body.html"
    _write_body(tmp_path, basename, "<p>Email: admin@evil.com</p>")
    _write_manifest(tmp_path, [{
        "basename": basename,
        "url": "https://evil.com/submit",
        "direction": "request",
        "method": "POST",
        "mime_type": "text/html",
        "capture_outcome": "ok",
    }])

    enricher = ResourceContentEnricher()
    results = await enricher.enrich(_ctx(tmp_path))
    assert results == []


@pytest.mark.asyncio
async def test_enricher_skips_non_text_mimes(tmp_path: Path) -> None:
    basename = "image.bin"
    _write_body(tmp_path, basename, "not text content")
    _write_manifest(tmp_path, [{
        "basename": basename,
        "url": "https://evil.com/image.png",
        "direction": "response",
        "method": "GET",
        "mime_type": "image/png",
        "capture_outcome": "ok",
    }])

    enricher = ResourceContentEnricher()
    results = await enricher.enrich(_ctx(tmp_path))
    assert results == []


@pytest.mark.asyncio
async def test_each_resource_links_to_its_own_domain(tmp_path: Path) -> None:
    """Phone on domainA and phone on domainB produce FOUND_ON links to their respective domains."""
    for basename, url, content in [
        ("a.html", "https://domainA.com/", "<p>(415) 867-5309</p>"),
        ("b.html", "https://domainB.com/", "<p>(415) 282-0700</p>"),
    ]:
        _write_body(tmp_path, basename, content)

    _write_manifest(tmp_path, [
        {"basename": "a.html", "url": "https://domainA.com/", "direction": "response",
         "method": "GET", "mime_type": "text/html", "capture_outcome": "ok"},
        {"basename": "b.html", "url": "https://domainB.com/", "direction": "response",
         "method": "GET", "mime_type": "text/html", "capture_outcome": "ok"},
    ])

    enricher = ResourceContentEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    domain_a_id = str(observable_id(ObservableType.DOMAIN, "domaina.com"))
    domain_b_id = str(observable_id(ObservableType.DOMAIN, "domainb.com"))

    all_links = [lk for r in results for lk in r.observable_links if lk.relationship == RelationshipType.FOUND_ON]
    targets = {str(lk.target_id) for lk in all_links}
    assert domain_a_id in targets
    assert domain_b_id in targets
