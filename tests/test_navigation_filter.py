"""Tests for navigation-scope extraction and noise filtering.

Fixture HAR topology
--------------------
    phish.evil.example.com/         (seed, 302, type=other)
       └─ redirect ─► /landing      (200, type=redirect)
            ├─ parser ─► /kit.js    (200, type=parser)
            │    └─ script ─► google-analytics.com/collect   (tracker — in scope but noise)
            └─ parser ─► /image.png (200, type=parser)

Orphaned / extra noise entries
    tracker.example.net/ping        (resource_type=ping, type=other — not in scope)
    cdn.unrelated.com/lib.js        (initiated by other.site.com — not in scope)

A navigations.json file seeds BFS roots.  With only the seed navigated the
topology matches the legacy single-seed behavior; an extra main-frame nav
injects a second root that reaches previously-orphan URLs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detonator.analysis.filter import (
    REASON_OUT_OF_SCOPE,
    REASON_RESOURCE_TYPE,
    REASON_TRACKER,
    NoiseFilter,
)
from detonator.analysis.navigation import (
    build_initiator_graph,
    extract_navigation_scope,
    parse_har,
    walk_from_roots,
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
                # Ping — noise resource type, also not in scope (type=other)
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
                # Completely unrelated — initiator is a URL outside the navigated set
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

# URLs reachable from the seed navigation via initiator graph
_SCOPE_URLS = {
    SEED_URL,
    "https://phish.evil.example.com/landing",
    "https://phish.evil.example.com/kit.js",
    "https://www.google-analytics.com/collect?v=1",
    "https://phish.evil.example.com/image.png",
}

_ORPHAN_URLS = {
    "https://tracker.example.net/ping",
    "https://cdn.unrelated.com/lib.js",
}


def _write_har(tmp_path: Path, data: dict | None = None) -> Path:
    har_path = tmp_path / "har_full.har"
    har_path.write_text(json.dumps(data or _HAR_FIXTURE), encoding="utf-8")
    return har_path


def _write_navigations(tmp_path: Path, urls: list[str]) -> Path:
    """Write a navigations.json with one main-frame entry per URL."""
    events = [
        {"timestamp": "2026-04-24T00:00:00Z", "url": u, "frame": "main"}
        for u in urls
    ]
    p = tmp_path / "navigations.json"
    p.write_text(json.dumps(events), encoding="utf-8")
    return p


# ── parse_har ─────────────────────────────────────────────────────────


def test_parse_har_entry_count(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    assert len(entries) == 7


def test_parse_har_redirect_initiator(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    landing = next(
        e for e in entries if e.url == "https://phish.evil.example.com/landing"
    )
    assert landing.initiator_type == "redirect"
    assert landing.initiator_url == SEED_URL


def test_parse_har_script_initiator(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    ga = next(e for e in entries if "google-analytics" in e.url)
    assert ga.initiator_type == "script"
    assert ga.initiator_url == "https://phish.evil.example.com/kit.js"


# ── build_initiator_graph ─────────────────────────────────────────────


def test_initiator_graph_edges(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    graph = build_initiator_graph(entries)
    assert "https://phish.evil.example.com/landing" in graph.get(SEED_URL, [])


# ── walk_from_roots ──────────────────────────────────────────────────


def test_walk_from_seed_only(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    reached = set(walk_from_roots(entries, [SEED_URL]))
    assert reached == _SCOPE_URLS


def test_walk_multiple_roots_unions(tmp_path: Path) -> None:
    entries = parse_har(_write_har(tmp_path))
    reached = set(
        walk_from_roots(entries, [SEED_URL, "https://other.site.com/page"])
    )
    # Seed scope + cdn.unrelated (child of other.site.com)
    assert _SCOPE_URLS.issubset(reached)
    assert "https://cdn.unrelated.com/lib.js" in reached


def test_walk_empty_entries() -> None:
    assert walk_from_roots([], [SEED_URL]) == []


def test_walk_unresolved_root_still_included(tmp_path: Path) -> None:
    """Root URLs that don't appear in the HAR should still land in scope."""
    entries = parse_har(_write_har(tmp_path))
    ghost = "https://ghost.example/path"
    reached = set(walk_from_roots(entries, [ghost]))
    assert ghost in reached


# ── extract_navigation_scope ─────────────────────────────────────────


def test_extract_scope_with_navigations(tmp_path: Path) -> None:
    nav_path = _write_navigations(tmp_path, [SEED_URL])
    scope = extract_navigation_scope(_write_har(tmp_path), nav_path, SEED_URL)
    assert scope is not None
    assert scope.seed_url == SEED_URL
    assert set(scope.scope_urls) == _SCOPE_URLS
    assert set(scope.out_of_scope_urls) == _ORPHAN_URLS


def test_extract_scope_without_navigations_falls_back_to_seed(tmp_path: Path) -> None:
    """When navigations.json is missing the seed URL is used as a lone root."""
    scope = extract_navigation_scope(_write_har(tmp_path), None, SEED_URL)
    assert scope is not None
    assert set(scope.scope_urls) == _SCOPE_URLS


def test_extract_scope_extra_navigation_adds_roots(tmp_path: Path) -> None:
    """A JS-driven main-frame nav that doesn't appear as an initiator child
    should still land in scope when present in navigations.json."""
    nav_path = _write_navigations(
        tmp_path, [SEED_URL, "https://other.site.com/page"]
    )
    scope = extract_navigation_scope(_write_har(tmp_path), nav_path, SEED_URL)
    assert scope is not None
    assert "https://cdn.unrelated.com/lib.js" in scope.scope_urls
    assert "https://other.site.com/page" in scope.scope_urls


def test_extract_scope_har_navigation_dict(tmp_path: Path) -> None:
    """har_navigation dict contains only scope entries."""
    nav_path = _write_navigations(tmp_path, [SEED_URL])
    scope = extract_navigation_scope(_write_har(tmp_path), nav_path, SEED_URL)
    assert scope is not None
    raw_urls = {
        e.get("request", {}).get("url")
        for e in scope.har_navigation.get("log", {}).get("entries", [])
    }
    assert raw_urls == _SCOPE_URLS


def test_extract_scope_missing_har(tmp_path: Path) -> None:
    scope = extract_navigation_scope(
        tmp_path / "nonexistent.json", None, SEED_URL
    )
    assert scope is None


# ── NoiseFilter ───────────────────────────────────────────────────────


def _scope(tmp_path: Path, nav_urls: list[str] | None = None):
    nav_path = _write_navigations(tmp_path, nav_urls or [SEED_URL])
    return extract_navigation_scope(_write_har(tmp_path), nav_path, SEED_URL)


def test_noise_filter_tracking_domain(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    ga = next(e for e in fr.entries if "google-analytics" in e.url)
    assert ga.is_noise is True
    assert REASON_TRACKER in ga.reasons


def test_noise_filter_resource_type_ping(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    ping = next(e for e in fr.entries if "tracker.example.net" in e.url)
    assert ping.is_noise is True
    assert REASON_RESOURCE_TYPE in ping.reasons


def test_noise_filter_out_of_scope_not_noise_by_default(tmp_path: Path) -> None:
    """Orphan entries are NOT noise by default (require_navigation_scope=False)."""
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    orphan = next(e for e in fr.entries if "cdn.unrelated.com" in e.url)
    assert orphan.is_noise is False
    assert orphan.in_scope is False
    assert REASON_OUT_OF_SCOPE not in orphan.reasons


def test_noise_filter_counts(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    assert fr.total_requests == 7
    # GA (tracker domain) + ping (noise resource type) = 2 noise
    # cdn.unrelated.com is out-of-scope but NOT noise by default
    assert fr.noise_requests == 2
    assert fr.scope_requests == 5


def test_noise_filter_extra_domain_config(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter(noise_domains=["phish.evil.example.com"]).run(scope, "run-1")

    phish = [e for e in fr.entries if "phish.evil.example.com" in e.url]
    assert all(e.is_noise for e in phish)


def test_noise_filter_har_navigation_excludes_noise(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    final_urls = {
        e.get("request", {}).get("url")
        for e in fr.har_navigation.get("log", {}).get("entries", [])
    }
    assert "https://www.google-analytics.com/collect?v=1" not in final_urls
    assert "https://tracker.example.net/ping" not in final_urls
    # Out-of-scope but non-noise — appears in output HAR
    assert "https://cdn.unrelated.com/lib.js" in final_urls
    # Navigation-scope URLs present
    assert SEED_URL in final_urls
    assert "https://phish.evil.example.com/landing" in final_urls


@pytest.mark.asyncio
async def test_filter_result_json_serialisable(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    raw = json.dumps(fr.model_dump(mode="json"))
    reloaded = json.loads(raw)
    assert reloaded["run_id"] == "run-1"
    assert reloaded["seed_url"] == SEED_URL
    assert isinstance(reloaded["entries"], list)


# ── require_navigation_scope behavior ────────────────────────────────


def test_require_navigation_scope_preserves_strict_behavior(tmp_path: Path) -> None:
    """With require_navigation_scope=True, out-of-scope entries become noise."""
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter(require_navigation_scope=True).run(scope, "run-1")

    orphan = next(e for e in fr.entries if "cdn.unrelated.com" in e.url)
    assert orphan.is_noise is True
    assert REASON_OUT_OF_SCOPE in orphan.reasons
    # GA (tracker, in-scope) + ping (out-of-scope + noise rtype) + cdn (out-of-scope)
    # = 3 noise; SEED, landing, kit.js, image.png = 4 in scope
    assert fr.noise_requests == 3
    assert fr.scope_requests == 4


def test_require_initiator_chain_alias(tmp_path: Path) -> None:
    """Legacy kwarg name still works (TOML configs still use it)."""
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter(require_initiator_chain=True).run(scope, "run-1")

    orphan = next(e for e in fr.entries if "cdn.unrelated.com" in e.url)
    assert orphan.is_noise is True
    assert REASON_OUT_OF_SCOPE in orphan.reasons


def test_in_scope_flag_set_for_reachable_entries(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert scope is not None
    fr = NoiseFilter().run(scope, "run-1")

    for url in _SCOPE_URLS:
        entry = next(e for e in fr.entries if e.url == url)
        assert entry.in_scope is True, f"{url} should have in_scope=True"

    orphan = next(e for e in fr.entries if "cdn.unrelated.com" in e.url)
    assert orphan.in_scope is False
