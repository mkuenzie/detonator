"""Tests for Phase D — Sigma evaluator with resource.* fields."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from detonator.analysis.modules.base import AnalysisContext, ResourceContent
from detonator.analysis.modules.sigma import SigmaModule


# ── Helpers ──────────────────────────────────────────────────────────

def _write_rule(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _minimal_ctx(resources: list[ResourceContent]) -> AnalysisContext:
    from detonator.analysis.chain import ChainResult, HarEntry
    from detonator.analysis.filter import FilterResult

    chain_result = ChainResult(
        seed_url="http://seed.com/",
        chain_urls=[],
        noise_urls=[],
        all_entries=[],
        chain_entries=[],
        noise_entries=[],
        har_chain={},
        har_all={},
    )
    filter_result = FilterResult(
        run_id="test",
        seed_url="http://seed.com/",
        total_requests=0,
        chain_requests=0,
        noise_requests=0,
        entries=[],
        har_chain={},
    )
    ctx = AnalysisContext(
        run_id="test-run",
        seed_url="http://seed.com/",
        seed_hostname="seed.com",
    )
    return ctx.model_copy(update={"resources": resources})


# ── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resource_body_contains_fires_per_matching_resource(tmp_path: Path) -> None:
    _write_rule(
        tmp_path,
        "atob.yml",
        """\
        title: Base64 eval
        id: aaaaaaaa-0000-0000-0000-000000000001
        description: Detects atob() in resource body
        signature_type: evasion
        confidence: 0.8
        detection:
          selection:
            resource.body|contains: "atob("
          condition: selection
        """,
    )
    resources = [
        ResourceContent(url="http://a.com/a.js", host="a.com", mime_type="text/javascript",
                        size_bytes=20, body="var x = atob('aGVsbG8=');"),
        ResourceContent(url="http://b.com/b.js", host="b.com", mime_type="text/javascript",
                        size_bytes=10, body="console.log('clean');"),
        ResourceContent(url="http://c.com/c.js", host="c.com", mime_type="text/javascript",
                        size_bytes=20, body="eval(atob('aGVsbG8='));"),
    ]
    ctx = _minimal_ctx(resources)
    module = SigmaModule(rules_dirs=[str(tmp_path)])
    hits = await module.analyze(ctx)

    # Two resources match — one hit each
    assert len(hits) == 2
    hit_urls = {h.evidence["resource_url"] for h in hits}
    assert "http://a.com/a.js" in hit_urls
    assert "http://c.com/c.js" in hit_urls
    assert "http://b.com/b.js" not in hit_urls


@pytest.mark.asyncio
async def test_resource_hit_carries_resource_evidence(tmp_path: Path) -> None:
    _write_rule(
        tmp_path,
        "body_rule.yml",
        """\
        title: window.location write
        id: aaaaaaaa-0000-0000-0000-000000000002
        description: Detects JS navigation
        signature_type: delivery
        confidence: 0.9
        detection:
          selection:
            resource.body|contains: "window.location"
          condition: selection
        """,
    )
    resources = [
        ResourceContent(url="http://a.com/a.js", host="a.com", mime_type="text/javascript",
                        size_bytes=30, body='window.location.href = "http://evil.com";'),
    ]
    ctx = _minimal_ctx(resources)
    module = SigmaModule(rules_dirs=[str(tmp_path)])
    hits = await module.analyze(ctx)

    assert len(hits) == 1
    assert hits[0].evidence["resource_url"] == "http://a.com/a.js"
    assert hits[0].evidence["mime_type"] == "text/javascript"


@pytest.mark.asyncio
async def test_non_resource_rule_evaluates_once(tmp_path: Path) -> None:
    """A rule without resource.* fields is still evaluated once against the context."""
    _write_rule(
        tmp_path,
        "domain_rule.yml",
        """\
        title: Storage domain
        id: aaaaaaaa-0000-0000-0000-000000000003
        description: Detects storage.googleapis.com
        signature_type: infrastructure
        confidence: 0.9
        detection:
          selection:
            chain.hostname|contains: "storage.googleapis.com"
          condition: selection
        """,
    )
    from detonator.analysis.chain import HarEntry
    from detonator.analysis.filter import FilterResult
    from detonator.analysis.modules.base import AnalysisContext

    ctx = AnalysisContext(
        run_id="test-run",
        seed_url="http://seed.com/",
        seed_hostname="seed.com",
        chain_hostnames=["storage.googleapis.com"],
    )
    module = SigmaModule(rules_dirs=[str(tmp_path)])
    hits = await module.analyze(ctx)

    assert len(hits) == 1
    assert hits[0].name == "Storage domain"


@pytest.mark.asyncio
async def test_resource_rule_no_match_produces_no_hit(tmp_path: Path) -> None:
    _write_rule(
        tmp_path,
        "atob_rule.yml",
        """\
        title: Base64 eval
        id: aaaaaaaa-0000-0000-0000-000000000004
        description: Detects atob
        signature_type: evasion
        confidence: 0.8
        detection:
          selection:
            resource.body|contains: "atob("
          condition: selection
        """,
    )
    resources = [
        ResourceContent(url="http://a.com/a.js", host="a.com", mime_type="text/javascript",
                        size_bytes=10, body="console.log('safe');"),
    ]
    ctx = _minimal_ctx(resources)
    module = SigmaModule(rules_dirs=[str(tmp_path)])
    hits = await module.analyze(ctx)
    assert hits == []


@pytest.mark.asyncio
async def test_resource_rule_with_empty_resources_produces_no_hit(tmp_path: Path) -> None:
    _write_rule(
        tmp_path,
        "atob_rule.yml",
        """\
        title: Base64 eval
        id: aaaaaaaa-0000-0000-0000-000000000005
        description: Detects atob
        signature_type: evasion
        confidence: 0.8
        detection:
          selection:
            resource.body|contains: "atob("
          condition: selection
        """,
    )
    ctx = _minimal_ctx(resources=[])
    module = SigmaModule(rules_dirs=[str(tmp_path)])
    hits = await module.analyze(ctx)
    assert hits == []
