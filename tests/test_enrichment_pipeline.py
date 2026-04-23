"""Tests for the Phase 4 enrichment pipeline.

Covers:
  - HAR extractor
  - TLD enricher (no I/O)
  - DOM extractor (no I/O)
  - EnrichmentPipeline.run() end-to-end against fixture artifacts
  - observable_id determinism
  - Pipeline skips gracefully when artifacts are missing
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from detonator.enrichment.base import EnrichmentResult, RunContext, observable_id
from detonator.enrichment.core.dom import DomExtractor
from detonator.enrichment.har import extract_from_har
from detonator.enrichment.pipeline import EnrichmentPipeline
from detonator.enrichment.plugins.tld import TldEnricher
from detonator.models.observables import ObservableType

# ── Fixture HAR ───────────────────────────────────────────────────

_HAR_FIXTURE = {
    "log": {
        "version": "1.2",
        "entries": [
            {
                "request": {"url": "https://evil.example.com/landing?q=1"},
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://evil.example.com/redirect"},
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://cdn.jsdelivr.net/npm/bootstrap@5/dist/bootstrap.min.js"},
                "serverIPAddress": "2.3.4.5",
            },
            {
                "request": {"url": "https://1.2.3.4/beacon"},  # raw IP as host — goes to ips
                "serverIPAddress": "1.2.3.4",
            },
        ],
    }
}

_DOM_FIXTURE = """<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="refresh" content="0; url=https://phish.evil.example.com/verify">
</head>
<body>
  <p>Contact us: victim@bank.com or call 555-867-5309</p>
  <p>Send BTC to: 1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf95</p>
  <p>Send ETH to: 0xde0B295669a9FD93d5F28D9Ec85E40f4cb697BAe</p>
  <form action="/submit" method="post">
    <input type="email" name="email">
    <button type="submit">Go</button>
  </form>
</body>
</html>
"""


# ── HAR extractor tests ────────────────────────────────────────────


def test_extract_from_har_domains_and_ips(tmp_path: Path) -> None:
    har_path = tmp_path / "har_full.har"
    har_path.write_text(json.dumps(_HAR_FIXTURE), encoding="utf-8")

    domains, ips, urls = extract_from_har(har_path)

    assert "evil.example.com" in domains
    assert "cdn.jsdelivr.net" in domains
    # Raw IP host should not appear in domains
    assert "1.2.3.4" not in domains
    assert "1.2.3.4" in ips
    assert "2.3.4.5" in ips
    assert any("landing" in u for u in urls)


def test_extract_from_har_missing_file(tmp_path: Path) -> None:
    domains, ips, urls = extract_from_har(tmp_path / "nonexistent.json")
    assert domains == [] and ips == [] and urls == []


def test_extract_from_har_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    domains, ips, urls = extract_from_har(p)
    assert domains == [] and ips == [] and urls == []


# ── observable_id determinism ─────────────────────────────────────


def test_observable_id_is_deterministic() -> None:
    a = observable_id("domain", "example.com")
    b = observable_id("domain", "example.com")
    assert a == b
    assert isinstance(a, uuid.UUID)


def test_observable_id_differs_by_type() -> None:
    a = observable_id("domain", "example.com")
    b = observable_id("ip", "example.com")
    assert a != b


def test_observable_id_case_insensitive() -> None:
    a = observable_id("domain", "Example.COM")
    b = observable_id("domain", "example.com")
    assert a == b


# ── TLD enricher ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tld_enricher_basic() -> None:
    enricher = TldEnricher()
    ctx = RunContext(run_id="r1", artifact_dir="/tmp", seed_url="https://evil.example.com")
    ctx.domains = ["evil.example.com", "cdn.jsdelivr.net"]

    results = await enricher.enrich(ctx)
    assert len(results) == 2

    by_domain = {r.input_value: r for r in results}

    evil = by_domain["evil.example.com"]
    assert evil.data["tld"] == "com"
    assert evil.data["label_count"] == 3
    assert evil.data["subdomain_depth"] == 1
    assert not evil.data["is_idn"]


@pytest.mark.asyncio
async def test_tld_enricher_idn() -> None:
    enricher = TldEnricher()
    ctx = RunContext(run_id="r1", artifact_dir="/tmp", seed_url="https://xn--e1afmapc.com")
    ctx.domains = ["xn--e1afmapc.com"]

    results = await enricher.enrich(ctx)
    assert results[0].data["is_idn"] is True


@pytest.mark.asyncio
async def test_tld_enricher_empty_context() -> None:
    enricher = TldEnricher()
    ctx = RunContext(run_id="r1", artifact_dir="/tmp", seed_url="https://example.com")
    results = await enricher.enrich(ctx)
    assert results == []


# ── DOM extractor ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dom_extractor_email(tmp_path: Path) -> None:
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    obs_by_type = {}
    for obs in result.observables:
        obs_by_type.setdefault(obs.type, []).append(obs.value)

    assert any("victim@bank.com" in v for v in obs_by_type.get(ObservableType.EMAIL, []))


@pytest.mark.asyncio
async def test_dom_extractor_phone(tmp_path: Path) -> None:
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)

    obs_types = {obs.type for obs in results[0].observables}
    assert ObservableType.PHONE in obs_types


@pytest.mark.asyncio
async def test_dom_extractor_crypto_wallets(tmp_path: Path) -> None:
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)

    obs_by_type = {}
    for obs in results[0].observables:
        obs_by_type.setdefault(obs.type, []).append(obs.value)

    wallets = obs_by_type.get(ObservableType.CRYPTO_WALLET, [])
    assert any("btc:" in w for w in wallets)
    assert any("eth:" in w for w in wallets)


@pytest.mark.asyncio
async def test_dom_extractor_form_action(tmp_path: Path) -> None:
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)

    assert "/submit" in results[0].data["form_actions"]


@pytest.mark.asyncio
async def test_dom_extractor_meta_refresh(tmp_path: Path) -> None:
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)

    refresh_urls = results[0].data["meta_refresh_urls"]
    assert any("phish.evil.example.com" in u for u in refresh_urls)


@pytest.mark.asyncio
async def test_dom_extractor_missing_file(tmp_path: Path) -> None:
    extractor = DomExtractor()
    ctx = RunContext(run_id="r1", artifact_dir=str(tmp_path), seed_url="https://evil.example.com")
    results = await extractor.enrich(ctx)
    # No dom.html → no results (silent skip)
    assert results == []


# ── Pipeline end-to-end ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_runs_enrichers_and_writes_json(tmp_path: Path) -> None:
    """Pipeline fans out to TLD + DOM enrichers against fixture artifacts."""
    (tmp_path / "har_full.har").write_text(json.dumps(_HAR_FIXTURE), encoding="utf-8")
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")

    mock_db = MagicMock()
    mock_db.upsert_observable = AsyncMock()
    mock_db.link_run_observable = AsyncMock()
    mock_db.link_observables = AsyncMock()
    mock_db.list_enrichment_exclusions = AsyncMock(return_value={})

    mock_store = MagicMock()

    from detonator.enrichment.core.dom import DomExtractor
    from detonator.enrichment.plugins.tld import TldEnricher

    pipeline = EnrichmentPipeline(
        enrichers=[TldEnricher(), DomExtractor()],
        database=mock_db,
        artifact_store=mock_store,
    )

    results = await pipeline.run("run-1", str(tmp_path), "https://evil.example.com/landing?q=1")

    # Should have results from both enrichers
    enricher_names = {r.enricher for r in results}
    assert "tld" in enricher_names
    assert "dom" in enricher_names

    # enrichment.json should be written
    enrich_json = tmp_path / "enrichment.json"
    assert enrich_json.exists()
    payload = json.loads(enrich_json.read_text())
    assert payload["run_id"] == "run-1"
    assert len(payload["results"]) == len(results)

    # DB observable upserts should have been called
    assert mock_db.upsert_observable.called


@pytest.mark.asyncio
async def test_pipeline_no_artifacts(tmp_path: Path) -> None:
    """Pipeline returns empty list when no artifact types are available."""
    mock_db = MagicMock()
    mock_store = MagicMock()

    pipeline = EnrichmentPipeline(
        enrichers=[TldEnricher(), DomExtractor()],
        database=mock_db,
        artifact_store=mock_store,
    )

    results = await pipeline.run("run-2", str(tmp_path), "https://example.com")
    assert results == []


@pytest.mark.asyncio
async def test_pipeline_failing_enricher_does_not_abort(tmp_path: Path) -> None:
    """A crash in one enricher should not abort the other enrichers."""
    (tmp_path / "har_full.har").write_text(json.dumps(_HAR_FIXTURE), encoding="utf-8")
    (tmp_path / "dom.html").write_text(_DOM_FIXTURE, encoding="utf-8")

    mock_db = MagicMock()
    mock_db.upsert_observable = AsyncMock()
    mock_db.link_run_observable = AsyncMock()
    mock_db.link_observables = AsyncMock()
    mock_db.list_enrichment_exclusions = AsyncMock(return_value={})

    # Build a broken enricher that always raises
    class _BrokenEnricher:
        name = "broken"

        def accepts(self, artifact_type: str) -> bool:
            return True

        async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
            raise RuntimeError("intentional failure")

    pipeline = EnrichmentPipeline(
        enrichers=[_BrokenEnricher(), TldEnricher()],  # type: ignore[list-item]
        database=mock_db,
        artifact_store=MagicMock(),
    )

    results = await pipeline.run("run-3", str(tmp_path), "https://evil.example.com")

    # TLD results should still be present
    assert any(r.enricher == "tld" for r in results)
    # Broken enricher should produce an error result
    assert any(r.enricher == "broken" and r.error for r in results)


def test_pipeline_build_from_config() -> None:
    """build_from_config wires known module names and skips unknowns."""
    from detonator.config import DetonatorConfig, EnrichmentConfig
    from detonator.enrichment.pipeline import EnrichmentPipeline

    cfg = DetonatorConfig(enrichment=EnrichmentConfig(modules=["tld", "dom", "nonexistent_module"]))
    mock_db = MagicMock()
    mock_store = MagicMock()

    pipeline = EnrichmentPipeline.build_from_config(cfg, mock_db, mock_store)
    names = [e.name for e in pipeline._enrichers]

    assert "tld" in names
    assert "dom" in names
    assert "nonexistent_module" not in names


# ── Exclusion tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tld_enricher_excludes_config_host() -> None:
    """Config-supplied exclude_hosts are filtered from TLD enricher."""
    from detonator.enrichment.plugins.tld import TldEnricher

    enricher = TldEnricher(exclude_hosts=["cdn.example.com"])
    ctx = RunContext(run_id="r1", artifact_dir="/tmp", seed_url="https://evil.example.com")
    ctx.domains = ["evil.example.com", "cdn.example.com"]

    results = await enricher.enrich(ctx)
    assert len(results) == 1
    assert results[0].input_value == "evil.example.com"


@pytest.mark.asyncio
async def test_exclusion_suffix_match() -> None:
    """Listing googleapis.com excludes fonts.googleapis.com but not googleapis.com.attacker.com."""
    from detonator.enrichment.plugins.tld import TldEnricher

    enricher = TldEnricher(exclude_hosts=["googleapis.com"])
    ctx = RunContext(run_id="r1", artifact_dir="/tmp", seed_url="https://attacker.com")
    ctx.domains = ["fonts.googleapis.com", "googleapis.com.attacker.com", "attacker.com"]

    results = await enricher.enrich(ctx)
    values = {r.input_value for r in results}

    # suffix match: fonts.googleapis.com is excluded
    assert "fonts.googleapis.com" not in values
    # tricky: attacker.com domain does NOT end with .googleapis.com
    assert "googleapis.com.attacker.com" in values
    assert "attacker.com" in values


@pytest.mark.asyncio
async def test_exclusion_per_enricher_independence() -> None:
    """Excluding a host from whois does not exclude it from tld."""
    from detonator.enrichment.plugins.tld import TldEnricher
    from detonator.enrichment.plugins.whois import WhoisEnricher

    whois = WhoisEnricher(exclude_hosts=["cdn.example.com"])
    tld = TldEnricher(exclude_hosts=[])

    assert whois._is_host_excluded("cdn.example.com")
    assert not tld._is_host_excluded("cdn.example.com")


def test_exclude_hosts_from_config_applied() -> None:
    """Hosts supplied via exclude_hosts are excluded; others are not."""
    from detonator.enrichment.plugins.whois import WhoisEnricher

    enricher = WhoisEnricher(exclude_hosts=["jsdelivr.net", "cloudflare.com"])

    assert enricher._is_host_excluded("jsdelivr.net")
    assert enricher._is_host_excluded("cdn.jsdelivr.net")
    assert not enricher._is_host_excluded("evil.example.com")


def test_empty_exclude_hosts_excludes_nothing() -> None:
    """With an empty list nothing is excluded."""
    from detonator.enrichment.plugins.whois import WhoisEnricher

    enricher = WhoisEnricher(exclude_hosts=[])
    assert not enricher._is_host_excluded("jsdelivr.net")
    assert not enricher._is_host_excluded("evil.example.com")
