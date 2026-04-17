"""Tests for the Sigma analysis module.

Covers each supported modifier (contains, startswith, endswith, re, gte, lte)
and each condition combinator (and / or / not), plus list-field OR semantics,
scalar-field matching, and rule-load-error handling.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from detonator.analysis.modules.base import AnalysisContext
from detonator.analysis.modules.sigma import SigmaModule


def _write_rule(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _ctx(**kwargs) -> AnalysisContext:
    defaults = dict(
        run_id="run-test",
        seed_url="https://evil.example.com/",
        seed_hostname="evil.example.com",
    )
    defaults.update(kwargs)
    return AnalysisContext(**defaults)


# ── Modifier: contains ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modifier_contains_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test contains match
        detection:
          sel:
            chain.hostname|contains: googleapis.com
          condition: sel
        signature_type: infrastructure
        confidence: 0.8
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["storage.googleapis.com", "example.com"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test contains match" for h in hits)


@pytest.mark.asyncio
async def test_modifier_contains_no_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test contains no match
        detection:
          sel:
            chain.hostname|contains: googleapis.com
          condition: sel
        signature_type: infrastructure
        confidence: 0.8
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["example.com", "other.net"])
    hits = await module.analyze(ctx)
    assert not hits


# ── Modifier: startswith ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modifier_startswith_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test startswith
        detection:
          sel:
            chain.url|startswith: "data:"
          condition: sel
        signature_type: evasion
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_urls=["data:text/html;base64,abc", "https://normal.com/"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test startswith" for h in hits)


@pytest.mark.asyncio
async def test_modifier_startswith_no_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test startswith no match
        detection:
          sel:
            chain.url|startswith: "data:"
          condition: sel
        signature_type: evasion
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_urls=["https://evil.com/page"])
    hits = await module.analyze(ctx)
    assert not hits


# ── Modifier: endswith ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modifier_endswith_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test endswith
        detection:
          sel:
            chain.hostname|endswith: .workers.dev
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["my-worker.evil.workers.dev"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test endswith" for h in hits)


@pytest.mark.asyncio
async def test_modifier_endswith_no_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test endswith no match
        detection:
          sel:
            chain.hostname|endswith: .workers.dev
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["legit.example.com"])
    hits = await module.analyze(ctx)
    assert not hits


# ── Modifier: re ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modifier_re_match(tmp_path: Path) -> None:
    # Use single-quoted YAML strings so backslashes are literal (no YAML escape processing).
    _write_rule(tmp_path, "rule.yml", """
        title: Test re
        detection:
          sel:
            seed.hostname|re: '^phish[0-9]+[.]evil[.]com$'
          condition: sel
        signature_type: infrastructure
        confidence: 0.85
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(seed_hostname="phish42.evil.com", seed_url="https://phish42.evil.com/")
    hits = await module.analyze(ctx)
    assert any(h.name == "Test re" for h in hits)


@pytest.mark.asyncio
async def test_modifier_re_no_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test re no match
        detection:
          sel:
            seed.hostname|re: '^phish[0-9]+[.]evil[.]com$'
          condition: sel
        signature_type: infrastructure
        confidence: 0.85
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(seed_hostname="legit.example.com", seed_url="https://legit.example.com/")
    hits = await module.analyze(ctx)
    assert not hits


# ── Modifier: gte / lte ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modifier_gte_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test gte
        detection:
          sel:
            chain.cross_origin_redirect_count|gte: 2
          condition: sel
        signature_type: delivery
        confidence: 0.75
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(cross_origin_redirect_count=3)
    hits = await module.analyze(ctx)
    assert any(h.name == "Test gte" for h in hits)


@pytest.mark.asyncio
async def test_modifier_gte_no_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test gte no match
        detection:
          sel:
            chain.cross_origin_redirect_count|gte: 2
          condition: sel
        signature_type: delivery
        confidence: 0.75
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(cross_origin_redirect_count=1)
    hits = await module.analyze(ctx)
    assert not hits


@pytest.mark.asyncio
async def test_modifier_lte_match(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test lte
        detection:
          sel:
            chain.cross_origin_redirect_count|lte: 5
          condition: sel
        signature_type: delivery
        confidence: 0.5
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(cross_origin_redirect_count=3)
    hits = await module.analyze(ctx)
    assert any(h.name == "Test lte" for h in hits)


# ── Condition combinators ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_condition_and_both_true(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test and both true
        detection:
          sel_host:
            chain.hostname|contains: docs.google.com
          sel_path:
            chain.url|contains: /forms/
          condition: sel_host and sel_path
        signature_type: delivery
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(
        chain_hostnames=["docs.google.com"],
        chain_urls=["https://docs.google.com/forms/d/abc/viewform"],
    )
    hits = await module.analyze(ctx)
    assert any(h.name == "Test and both true" for h in hits)


@pytest.mark.asyncio
async def test_condition_and_one_false(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test and one false
        detection:
          sel_host:
            chain.hostname|contains: docs.google.com
          sel_path:
            chain.url|contains: /forms/
          condition: sel_host and sel_path
        signature_type: delivery
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    # hostname matches but no /forms/ in urls
    ctx = _ctx(
        chain_hostnames=["docs.google.com"],
        chain_urls=["https://docs.google.com/spreadsheets/d/abc"],
    )
    hits = await module.analyze(ctx)
    assert not hits


@pytest.mark.asyncio
async def test_condition_or(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test or
        detection:
          sel_a:
            chain.hostname|endswith: .github.io
          sel_b:
            chain.hostname|endswith: .workers.dev
          condition: sel_a or sel_b
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["evil.github.io"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test or" for h in hits)


@pytest.mark.asyncio
async def test_condition_not(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test not
        detection:
          sel_legit:
            chain.hostname|contains: legit.example.com
          condition: not sel_legit
        signature_type: infrastructure
        confidence: 0.5
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["evil.example.com"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test not" for h in hits)


@pytest.mark.asyncio
async def test_condition_not_inverted(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test not inverted
        detection:
          sel_legit:
            chain.hostname|contains: legit.example.com
          condition: not sel_legit
        signature_type: infrastructure
        confidence: 0.5
    """)
    module = SigmaModule([str(tmp_path)])
    # The hostname IS legit.example.com so 'not' should prevent a hit
    ctx = _ctx(chain_hostnames=["legit.example.com"])
    hits = await module.analyze(ctx)
    assert not hits


# ── List-field OR semantics ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_field_any_element_matches(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test list any
        detection:
          sel:
            chain.hostname|endswith: .sharepoint.com
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    # Only one of several hostnames matches
    ctx = _ctx(chain_hostnames=["legit.example.com", "tenant.sharepoint.com", "cdn.net"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test list any" for h in hits)


# ── Rule value list = OR ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_value_list_or(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Test value list or
        detection:
          sel:
            chain.hostname|endswith:
              - .github.io
              - .workers.dev
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["evil.workers.dev"])
    hits = await module.analyze(ctx)
    assert any(h.name == "Test value list or" for h in hits)


# ── Unsupported modifier → rule skipped ──────────────────────────────


def test_unsupported_modifier_skipped(tmp_path: Path) -> None:
    _write_rule(tmp_path, "bad.yml", """
        title: Bad modifier
        detection:
          sel:
            chain.hostname|cidr: 192.168.0.0/16
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    assert len(module._rules) == 0


# ── Missing condition → rule skipped ─────────────────────────────────


def test_missing_condition_skipped(tmp_path: Path) -> None:
    _write_rule(tmp_path, "no_cond.yml", """
        title: No condition
        detection:
          sel:
            chain.hostname|contains: evil.com
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    assert len(module._rules) == 0


# ── Unknown field → rule skipped ─────────────────────────────────────


def test_unknown_field_skipped(tmp_path: Path) -> None:
    _write_rule(tmp_path, "bad_field.yml", """
        title: Unknown field
        detection:
          sel:
            unknown.field|contains: something
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    assert len(module._rules) == 0


# ── Explicit UUID id preserved ────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_uuid_id_used(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule.yml", """
        title: Rule with explicit id
        id: 12345678-1234-5678-1234-567812345678
        detection:
          sel:
            chain.hostname|contains: evil.com
          condition: sel
        signature_type: infrastructure
        confidence: 0.9
    """)
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx(chain_hostnames=["evil.com"])
    hits = await module.analyze(ctx)
    assert hits
    assert hits[0].technique_id == "12345678-1234-5678-1234-567812345678"


# ── Empty rules_dir is tolerated ──────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_rules_dir(tmp_path: Path) -> None:
    module = SigmaModule([str(tmp_path)])
    ctx = _ctx()
    hits = await module.analyze(ctx)
    assert hits == []


# ── Nonexistent rules_dir is tolerated ───────────────────────────────


def test_nonexistent_rules_dir() -> None:
    module = SigmaModule(["/nonexistent/path/to/rules"])
    assert module._rules == []
