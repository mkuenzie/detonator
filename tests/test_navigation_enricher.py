"""Tests for Phase C — NavigationEnricher (redirects_to observable links)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detonator.enrichment.base import RunContext
from detonator.enrichment.core.navigations import NavigationEnricher
from detonator.models.observables import RelationshipType


# ── Fixtures ──────────────────────────────────────────────────────────

def _write_navigations(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "navigations.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    data = {"log": {"version": "1.2", "entries": entries}}
    p = tmp_path / "har_full.har"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="test-run",
        artifact_dir=str(tmp_path),
        seed_url="http://seed.com/",
    )


_NAV_ABC = [
    {"timestamp": "2024-01-01T00:00:00+00:00", "url": "http://seed.com/", "frame": "main"},
    {"timestamp": "2024-01-01T00:00:01+00:00", "url": "http://intermediate.com/", "frame": "main"},
    {"timestamp": "2024-01-01T00:00:02+00:00", "url": "http://evil.com/", "frame": "main"},
]

_HAR_ENTRIES = [
    {
        "request": {"url": "http://seed.com/"},
        "response": {"status": 200},
        "_initiator": {"type": "other"},
    },
    {
        "request": {"url": "http://intermediate.com/"},
        "response": {"status": 200},
        "_initiator": {"type": "script"},
    },
    {
        "request": {"url": "http://evil.com/"},
        "response": {"status": 302},
        "_initiator": {"type": "redirect"},
    },
]


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_navigation_enricher_accepts_navigations() -> None:
    e = NavigationEnricher()
    assert e.accepts("navigations") is True
    assert e.accepts("har") is False
    assert e.accepts("dom") is False


@pytest.mark.asyncio
async def test_cross_host_hops_produce_links(tmp_path: Path) -> None:
    _write_navigations(tmp_path, _NAV_ABC)
    _write_har(tmp_path, _HAR_ENTRIES)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    # Two cross-host hops: seed.com → intermediate.com and intermediate.com → evil.com
    assert len(result.observable_links) == 2
    rels = {link.relationship for link in result.observable_links}
    assert RelationshipType.REDIRECTS_TO in rels


@pytest.mark.asyncio
async def test_trigger_script_from_har(tmp_path: Path) -> None:
    _write_navigations(tmp_path, _NAV_ABC)
    _write_har(tmp_path, _HAR_ENTRIES)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    links = results[0].observable_links
    seed_to_intermediate = next(
        lk for lk in links if "intermediate.com" in str(lk.evidence.get("next_url", ""))
    )
    assert seed_to_intermediate.evidence["trigger"] == "script"


@pytest.mark.asyncio
async def test_trigger_redirect_from_har(tmp_path: Path) -> None:
    _write_navigations(tmp_path, _NAV_ABC)
    _write_har(tmp_path, _HAR_ENTRIES)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    links = results[0].observable_links
    intermediate_to_evil = next(
        lk for lk in links if "evil.com" in str(lk.evidence.get("next_url", ""))
    )
    assert intermediate_to_evil.evidence["trigger"] == "redirect"


@pytest.mark.asyncio
async def test_same_host_navigation_produces_no_link(tmp_path: Path) -> None:
    navs = [
        {"timestamp": "2024-01-01T00:00:00+00:00", "url": "http://seed.com/page1", "frame": "main"},
        {"timestamp": "2024-01-01T00:00:01+00:00", "url": "http://seed.com/page2", "frame": "main"},
    ]
    _write_navigations(tmp_path, navs)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    assert len(results[0].observable_links) == 0


@pytest.mark.asyncio
async def test_consecutive_duplicate_urls_dedupe(tmp_path: Path) -> None:
    navs = [
        {"timestamp": "2024-01-01T00:00:00+00:00", "url": "http://seed.com/", "frame": "main"},
        {"timestamp": "2024-01-01T00:00:01+00:00", "url": "http://seed.com/", "frame": "main"},
        {"timestamp": "2024-01-01T00:00:02+00:00", "url": "http://other.com/", "frame": "main"},
    ]
    _write_navigations(tmp_path, navs)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    # After dedup: [seed.com, other.com] → one hop
    assert len(results[0].observable_links) == 1


@pytest.mark.asyncio
async def test_sub_frame_navigations_ignored(tmp_path: Path) -> None:
    navs = [
        {"timestamp": "2024-01-01T00:00:00+00:00", "url": "http://seed.com/", "frame": "main"},
        {"timestamp": "2024-01-01T00:00:01+00:00", "url": "http://iframe.com/ad", "frame": "sub"},
        {"timestamp": "2024-01-01T00:00:02+00:00", "url": "http://other.com/", "frame": "main"},
    ]
    _write_navigations(tmp_path, navs)
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))

    # Only main-frame navigations: seed.com → other.com
    assert len(results[0].observable_links) == 1


@pytest.mark.asyncio
async def test_missing_navigations_json_returns_error(tmp_path: Path) -> None:
    enricher = NavigationEnricher()
    results = await enricher.enrich(_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].error is not None
