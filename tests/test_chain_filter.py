"""Tests for Phase 5 — chain extraction and noise filtering.

Fixture HAR topology
--------------------
    phish.evil.example.com/         (seed, 302, type=other)
       └─ redirect ─► /landing      (200, type=redirect)
            ├─ parser ─► /kit.js    (200, type=parser)
            │    └─ script ─► google-analytics.com/collect   (tracker — in chain but noise)
            └─ parser ─► /image.png (200, type=parser)

Orphaned / extra noise entries
    tracker.example.net/ping        (resource_type=ping, type=other — not in chain)
    cdn.unrelated.com/lib.js        (initiated by other.site.com — not in chain)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from detonator.analysis.chain import (
    HarEntry,
    build_initiator_graph,
    extract_chain,
    parse_har,
    walk_chain,
)
from detonator.analysis.filter import (
    REASON_NO_CHAIN,
    REASON_RESOURCE_TYPE,
    REASON_TRACKER,
    NoiseFilter,
    TechniqueDetector,
)

# ── Fixture HAR ───────────────────────────────────────────────────────

SEED_URL = "https://phish.evil.example.com/"

_HAR_FIXTURE: dict = {
    "log": {
        "version": "1.2",
        "creator": {"name": "Playwright", "version": "1.x"},
        "entries": [
            {
                "request": {"url": SEED_URL, "method": "GET"},
                "response": {"status": 302, "content": {"mimeType": "text/html"}},
                "_initiator": {"type": "other"},
                "_resourceType": "document",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {
                    "url": "https://phish.evil.example.com/landing",
                    "method": "GET",
                },
                "response": {"status": 200, "content": {"mimeType": "text/html"}},
                "_initiator": {"type": "redirect", "url": SEED_URL},
                "_resourceType": "document",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {
                    "url": "https://phish.evil.example.com/kit.js",
                    "method": "GET",
                },
                "response": {
                    "status": 200,
                    "content": {"mimeType": "application/javascript"},
                },
                "_initiator": {
                    "type": "parser",
                    "url": "https://phish.evil.example.com/landing",
                },
                "_resourceType": "script",
                "serverIPAddress": "1.2.3.4",
            },
            {
                # Google Analytics — reachable via script initiator, but tracking noise
                "request": {
                    "url": "https://www.google-analytics.com/collect?v=1",
                    "method": "GET",
                },
                "response": {"status": 200, "content": {"mimeType": "image/gif"}},
                "_initiator": {
                    "type": "script",
                    "stack": {
                        "callFrames": [
                            {"url": "https://phish.evil.example.com/kit.js"}
                        ]
                    },
                },
                "_resourceType": "xhr",
                "serverIPAddress": "2.3.4.5",
            },
            {
                "request": {
                    "url": "https://phish.evil.example.com/image.png",
                    "method": "GET",
                },
                "response": {"status": 200, "content": {"mimeType": "image/png"}},
                "_initiator": {
                    "type": "parser",
                    "url": "https://phish.evil.example.com/landing",
                },
                "_resourceType": "image",
                "serverIPAddress": "1.2.3.4",
            },
            {
                # Ping — noise resource type, also not in chain (type=other)
                "request": {
                    "url": "https://tracker.example.net/ping",
                    "method": "POST",
                },
                "response": {"status": 204, "content": {}},
                "_initiator": {"type": "other"},
                "_resourceType": "ping",
                "serverIPAddress": "3.4.5.6",
            },
            {
                # Completely unrelated — initiator is a URL outside the chain
                "request": {
                    "url": "https://cdn.unrelated.com/lib.js",
                    "method": "GET",
                },
                "response": {
                    "status": 200,
                    "content": {"mimeType": "application/javascript"},
                },
                "_initiator": {
                    "type": "script",
                    "stack": {
                        "callFrames": [{"url": "https://other.site.com/page"}]
                    },
                },
                "_resourceType": "script",
                "serverIPAddress": "4.5.6.7",
            },
        ],
    }
}

# URLs we expect to be in the chain (initiator-reachable from seed)
_CHAIN_URLS = {
    SEED_URL,
    "https://phish.evil.example.com/landing",
    "https://phish.evil.example.com/kit.js",
    "https://www.google-analytics.com/collect?v=1",
    "https://phish.evil.example.com/image.png",
}

_NOISE_URLS = {
    "https://tracker.example.net/ping",
    "https://cdn.unrelated.com/lib.js",
}


def _write_har(tmp_path: Path, data: dict | None = None) -> Path:
    har_path = tmp_path / "har_full.har"
    har_path.write_text(json.dumps(data or _HAR_FIXTURE), encoding="utf-8")
    return har_path


# ── parse_har ─────────────────────────────────────────────────────────


def test_parse_har_entry_count(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    assert len(entries) == 7


def test_parse_har_seed_entry(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    seed = next(e for e in entries if e.url == SEED_URL)
    assert seed.initiator_type == "other"
    assert seed.initiator_url is None
    assert seed.response_status == 302
    assert seed.server_ip == "1.2.3.4"


def test_parse_har_redirect_entry(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    landing = next(
        e for e in entries if e.url == "https://phish.evil.example.com/landing"
    )
    assert landing.initiator_type == "redirect"
    assert landing.initiator_url == SEED_URL


def test_parse_har_script_initiator(tmp_path: Path) -> None:
    """Script initiator URL extracted from callFrames."""
    entries = parse_har(_write_har(tmp_path))
    ga = next(
        e for e in entries if "google-analytics" in e.url
    )
    assert ga.initiator_type == "script"
    assert ga.initiator_url == "https://phish.evil.example.com/kit.js"


def test_parse_har_missing_file(tmp_path: Path) -> None:
    entries = parse_har(tmp_path / "nonexistent.json")
    assert entries == []


def test_parse_har_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    entries = parse_har(p)
    assert entries == []


# ── build_initiator_graph ─────────────────────────────────────────────


def test_initiator_graph_edges(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    graph = build_initiator_graph(entries)

    # seed initiated landing (redirect)
    assert "https://phish.evil.example.com/landing" in graph.get(SEED_URL, [])
    # landing initiated kit.js and image.png
    landing_children = graph.get("https://phish.evil.example.com/landing", [])
    assert "https://phish.evil.example.com/kit.js" in landing_children
    assert "https://phish.evil.example.com/image.png" in landing_children
    # kit.js initiated GA
    assert "https://www.google-analytics.com/collect?v=1" in graph.get(
        "https://phish.evil.example.com/kit.js", []
    )


def test_initiator_graph_orphan_absent(tmp_path: Path) -> None:
    """Orphaned entries have no parent edge in the graph."""
    entries = parse_har(_write_har(tmp_path))
    graph = build_initiator_graph(entries)
    all_children = {url for children in graph.values() for url in children}
    # cdn.unrelated.com is *in* all_children only via other.site.com, not via our chain
    # tracker.example.net/ping has type=other so no initiator_url at all
    assert "https://tracker.example.net/ping" not in all_children


# ── walk_chain ────────────────────────────────────────────────────────


def test_walk_chain_includes_chain_urls(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    chain = set(walk_chain(entries, SEED_URL))
    assert _CHAIN_URLS == chain


def test_walk_chain_excludes_noise(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    chain = set(walk_chain(entries, SEED_URL))
    for url in _NOISE_URLS:
        assert url not in chain


def test_walk_chain_follows_redirect(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    chain = set(walk_chain(entries, SEED_URL))
    assert "https://phish.evil.example.com/landing" in chain


def test_walk_chain_follows_script_initiator(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    chain = set(walk_chain(entries, SEED_URL))
    assert "https://www.google-analytics.com/collect?v=1" in chain


def test_walk_chain_seed_url_normalisation(tmp_path: Path) -> None:
    """Seed URL with trailing slash should still match."""
    entries = parse_har(_write_har(tmp_path))
    chain = set(walk_chain(entries, SEED_URL.rstrip("/")))
    # Should still resolve to the correct start and build the full chain
    assert "https://phish.evil.example.com/landing" in chain


def test_walk_chain_empty_entries() -> None:
    assert walk_chain([], SEED_URL) == []


# ── extract_chain ─────────────────────────────────────────────────────


def test_extract_chain_result_structure(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    assert result is not None
    assert result.seed_url == SEED_URL
    assert set(result.chain_urls) == _CHAIN_URLS
    assert set(result.noise_urls) == _NOISE_URLS


def test_extract_chain_entries_split(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    assert result is not None
    chain_url_set = {e.url for e in result.chain_entries}
    noise_url_set = {e.url for e in result.noise_entries}
    assert chain_url_set == _CHAIN_URLS
    assert noise_url_set == _NOISE_URLS


def test_extract_chain_har_chain_dict(tmp_path: Path) -> None:
    """har_chain dict should only contain chain entries."""
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    assert result is not None
    raw_urls = {
        e.get("request", {}).get("url")
        for e in result.har_chain.get("log", {}).get("entries", [])
    }
    assert raw_urls == _CHAIN_URLS


def test_extract_chain_missing_har(tmp_path: Path) -> None:
    result = extract_chain(tmp_path / "nonexistent.json", SEED_URL)
    assert result is None


# ── NoiseFilter ───────────────────────────────────────────────────────


def test_noise_filter_tracking_domain(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    ga_entry = next(
        e for e in fr.entries if "google-analytics" in e.url
    )
    assert ga_entry.is_noise is True
    assert REASON_TRACKER in ga_entry.reasons


def test_noise_filter_resource_type_ping(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    ping_entry = next(e for e in fr.entries if "tracker.example.net" in e.url)
    assert ping_entry.is_noise is True
    assert REASON_RESOURCE_TYPE in ping_entry.reasons


def test_noise_filter_no_chain(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    orphan = next(e for e in fr.entries if "cdn.unrelated.com" in e.url)
    assert orphan.is_noise is True
    assert REASON_NO_CHAIN in orphan.reasons


def test_noise_filter_chain_entries_clean(tmp_path: Path) -> None:
    """phish.evil.example.com entries (excl. GA) should not be noise."""
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    clean = {e.url for e in fr.entries if not e.is_noise}
    assert SEED_URL in clean
    assert "https://phish.evil.example.com/landing" in clean
    assert "https://phish.evil.example.com/kit.js" in clean
    assert "https://phish.evil.example.com/image.png" in clean


def test_noise_filter_counts(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    assert fr.total_requests == 7
    # GA + ping + unrelated = 3 noise
    assert fr.noise_requests == 3
    assert fr.chain_requests == 4


def test_noise_filter_extra_domain_config(tmp_path: Path) -> None:
    """noise_domains config supplements default list."""
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter(noise_domains=["phish.evil.example.com"])
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    # All phish.evil.example.com entries should now be noise too
    phish_entries = [e for e in fr.entries if "phish.evil.example.com" in e.url]
    assert all(e.is_noise for e in phish_entries)


def test_noise_filter_har_chain_excludes_noise(tmp_path: Path) -> None:
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    final_urls = {
        e.get("request", {}).get("url")
        for e in fr.har_chain.get("log", {}).get("entries", [])
    }
    # GA filtered, tracker.net filtered, cdn.unrelated.com filtered
    assert "https://www.google-analytics.com/collect?v=1" not in final_urls
    assert "https://tracker.example.net/ping" not in final_urls
    assert "https://cdn.unrelated.com/lib.js" not in final_urls
    # Phishing chain URLs present
    assert SEED_URL in final_urls
    assert "https://phish.evil.example.com/landing" in final_urls


# ── TechniqueDetector ─────────────────────────────────────────────────


def _gcs_entry() -> HarEntry:
    return HarEntry(
        url="https://storage.googleapis.com/phish-bucket/page.html",
        method="GET",
        resource_type="document",
    )


def _workers_entry() -> HarEntry:
    return HarEntry(
        url="https://my-worker.my-account.workers.dev/",
        method="GET",
        resource_type="document",
    )


def _redirect_entry(url: str, from_url: str) -> HarEntry:
    return HarEntry(
        url=url,
        method="GET",
        resource_type="document",
        initiator_type="redirect",
        initiator_url=from_url,
    )


def test_technique_detection_gcs() -> None:
    detector = TechniqueDetector()
    hits = detector.detect([_gcs_entry()], "run-1")
    names = {h.name for h in hits}
    assert "Google Cloud Storage phishing host" in names


def test_technique_detection_workers_dev() -> None:
    detector = TechniqueDetector()
    hits = detector.detect([_workers_entry()], "run-1")
    names = {h.name for h in hits}
    assert "Cloudflare Workers abuse" in names


def test_technique_detection_cross_origin_redirect() -> None:
    detector = TechniqueDetector()
    entries = [
        _redirect_entry("https://track.redirect1.com/hop", "https://landing.evil.com/"),
        _redirect_entry("https://final.evil.net/page", "https://track.redirect1.com/hop"),
    ]
    hits = detector.detect(entries, "run-1")
    names = {h.name for h in hits}
    assert "Cross-origin redirect chain" in names


def test_technique_detection_no_hits() -> None:
    detector = TechniqueDetector()
    entries = [
        HarEntry(
            url="https://phish.evil.example.com/",
            resource_type="document",
            initiator_type="other",
        )
    ]
    hits = detector.detect(entries, "run-1")
    assert hits == []


def test_technique_ids_are_deterministic() -> None:
    """Same technique name should always produce the same UUID."""
    detector = TechniqueDetector()
    hits1 = detector.detect([_gcs_entry()], "run-a")
    hits2 = detector.detect([_gcs_entry()], "run-b")
    assert hits1[0].technique_id == hits2[0].technique_id
    assert uuid.UUID(hits1[0].technique_id)  # valid UUID


# ── end-to-end filter_result.json round-trip ──────────────────────────


@pytest.mark.asyncio
async def test_filter_result_json_serialisable(tmp_path: Path) -> None:
    """FilterResult.model_dump(mode='json') should round-trip through json.dumps."""
    result = extract_chain(_write_har(tmp_path), SEED_URL)
    nf = NoiseFilter()
    fr = nf.run(result, "run-1")  # type: ignore[arg-type]

    raw = json.dumps(fr.model_dump(mode="json"))
    reloaded = json.loads(raw)
    assert reloaded["run_id"] == "run-1"
    assert reloaded["seed_url"] == SEED_URL
    assert isinstance(reloaded["entries"], list)
