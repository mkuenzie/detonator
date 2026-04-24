"""Tests for Phase D — ResourceContent loading in AnalysisContext.from_navigation_scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detonator.analysis.modules.base import (
    AnalysisContext,
    ResourceContent,
    _MAX_BODY,
    _TEXTY_MIMES,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_har(tmp_path: Path, entries: list[dict]) -> Path:
    data = {"log": {"version": "1.2", "entries": entries}}
    p = tmp_path / "har_full.har"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _make_artifact(
    tmp_path: Path,
    filename: str,
    body: str,
    source_url: str,
    mime: str,
    artifact_type: str = "site_resource",
) -> dict:
    p = tmp_path / filename
    p.write_text(body, encoding="utf-8")
    return {
        "artifact_type": artifact_type,
        "source_url": source_url,
        "path": str(p),
        "size": len(body.encode()),
    }


def _ctx_from_artifacts(
    tmp_path: Path,
    artifacts: list[dict],
    har_entries: list[dict] | None = None,
) -> AnalysisContext:
    """Build an AnalysisContext with minimal navigation-scope/filter stubs."""
    from detonator.analysis.filter import FilterResult
    from detonator.analysis.navigation import NavigationScope

    nav_scope = NavigationScope(
        seed_url="http://seed.com/",
        navigation_events=[],
        navigation_urls=[],
        navigation_hosts=[],
        scope_urls=[],
        out_of_scope_urls=[],
        all_entries=[],
        scope_entries=[],
        out_of_scope_entries=[],
        har_full={},
        har_navigation={},
    )
    filter_result = FilterResult(
        run_id="test",
        seed_url="http://seed.com/",
        total_requests=0,
        scope_requests=0,
        noise_requests=0,
        entries=[],
        har_navigation={},
    )
    if har_entries:
        _make_har(tmp_path, har_entries)

    return AnalysisContext.from_navigation_scope(
        nav_scope,
        filter_result,
        str(tmp_path),
        "test-run",
        "http://seed.com/",
        artifacts=artifacts,
    )


# ── Tests ─────────────────────────────────────────────────────────────

def test_html_site_resource_loaded(tmp_path: Path) -> None:
    html_body = "<html><body>hello</body></html>"
    artifacts = [_make_artifact(tmp_path, "page.html", html_body, "http://seed.com/page", "text/html")]
    har_entries = [
        {
            "request": {"url": "http://seed.com/page"},
            "response": {"content": {"mimeType": "text/html"}},
        }
    ]
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries)

    assert len(ctx.resources) == 1
    r = ctx.resources[0]
    assert r.url == "http://seed.com/page"
    assert r.mime_type == "text/html"
    assert r.body == html_body
    assert r.host == "seed.com"


def test_image_resource_excluded(tmp_path: Path) -> None:
    artifacts = [_make_artifact(tmp_path, "img.png", "PNGDATA", "http://seed.com/img.png", "image/png")]
    har_entries = [
        {
            "request": {"url": "http://seed.com/img.png"},
            "response": {"content": {"mimeType": "image/png"}},
        }
    ]
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries)
    assert len(ctx.resources) == 0


def test_oversized_resource_excluded(tmp_path: Path) -> None:
    big_body = "x" * (_MAX_BODY + 1)
    artifacts = [
        {
            "artifact_type": "site_resource",
            "source_url": "http://seed.com/big.js",
            "path": str(tmp_path / "big.js"),
            "size": _MAX_BODY + 1,
        }
    ]
    (tmp_path / "big.js").write_text(big_body, encoding="utf-8")
    har_entries = [
        {
            "request": {"url": "http://seed.com/big.js"},
            "response": {"content": {"mimeType": "text/javascript"}},
        }
    ]
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries)
    assert len(ctx.resources) == 0


def test_non_site_resource_excluded(tmp_path: Path) -> None:
    artifacts = [_make_artifact(tmp_path, "dom.html", "<html/>", "http://seed.com/", "text/html", artifact_type="dom")]
    har_entries = [
        {
            "request": {"url": "http://seed.com/"},
            "response": {"content": {"mimeType": "text/html"}},
        }
    ]
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries)
    assert len(ctx.resources) == 0


def test_multiple_text_resources_loaded(tmp_path: Path) -> None:
    artifacts = [
        _make_artifact(tmp_path, "a.html", "<html>a</html>", "http://a.com/a", "text/html"),
        _make_artifact(tmp_path, "b.js", "alert('b')", "http://b.com/b.js", "text/javascript"),
    ]
    har_entries = [
        {"request": {"url": "http://a.com/a"}, "response": {"content": {"mimeType": "text/html"}}},
        {"request": {"url": "http://b.com/b.js"}, "response": {"content": {"mimeType": "text/javascript"}}},
    ]
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries)
    assert len(ctx.resources) == 2
    urls = {r.url for r in ctx.resources}
    assert "http://a.com/a" in urls
    assert "http://b.com/b.js" in urls


def test_missing_har_leaves_resources_empty(tmp_path: Path) -> None:
    """No har_full.har → MIME map is empty → no resources loaded."""
    artifacts = [
        {
            "artifact_type": "site_resource",
            "source_url": "http://seed.com/page",
            "path": str(tmp_path / "page.html"),
            "size": 10,
        }
    ]
    (tmp_path / "page.html").write_text("<html/>", encoding="utf-8")
    ctx = _ctx_from_artifacts(tmp_path, artifacts, har_entries=None)
    assert len(ctx.resources) == 0
