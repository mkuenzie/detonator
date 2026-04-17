"""Tests for the AnalysisPipeline orchestration logic.

Focus: exception swallowing, hit aggregation, deduplication.
"""

from __future__ import annotations

import pytest

from detonator.analysis.modules.base import AnalysisContext, AnalysisModule, TechniqueHit
from detonator.analysis.modules.pipeline import AnalysisPipeline


# ── Fake module helpers ───────────────────────────────────────────────

def _hit(technique_id: str, name: str = "Test Technique", confidence: float = 0.9) -> TechniqueHit:
    return TechniqueHit(
        technique_id=technique_id,
        name=name,
        description="desc",
        signature_type="infrastructure",
        confidence=confidence,
        evidence={},
        detection_module="test",
    )


class _ConstModule(AnalysisModule):
    def __init__(self, module_name: str, hits: list[TechniqueHit]) -> None:
        self._name = module_name
        self._hits = hits

    @property
    def name(self) -> str:
        return self._name

    async def analyze(self, context: AnalysisContext) -> list[TechniqueHit]:
        return self._hits


class _RaisingModule(AnalysisModule):
    @property
    def name(self) -> str:
        return "raising"

    async def analyze(self, context: AnalysisContext) -> list[TechniqueHit]:
        raise RuntimeError("simulated module failure")


def _ctx() -> AnalysisContext:
    return AnalysisContext(
        run_id="run-test",
        seed_url="https://evil.example.com/",
        seed_hostname="evil.example.com",
    )


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_swallows_exception_and_returns_other_hits() -> None:
    """A module that raises must not suppress hits from other modules."""
    good_hits = [_hit("tid-1"), _hit("tid-2")]
    pipeline = AnalysisPipeline(
        modules=[_RaisingModule(), _ConstModule("good", good_hits)]
    )
    hits = await pipeline.run(_ctx())
    assert len(hits) == 2
    ids = {h.technique_id for h in hits}
    assert ids == {"tid-1", "tid-2"}


@pytest.mark.asyncio
async def test_pipeline_empty_modules() -> None:
    pipeline = AnalysisPipeline(modules=[])
    hits = await pipeline.run(_ctx())
    assert hits == []


@pytest.mark.asyncio
async def test_pipeline_aggregates_hits_from_two_modules() -> None:
    m1 = _ConstModule("m1", [_hit("tid-1")])
    m2 = _ConstModule("m2", [_hit("tid-2")])
    pipeline = AnalysisPipeline(modules=[m1, m2])
    hits = await pipeline.run(_ctx())
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_pipeline_deduplicate_first_writer_wins() -> None:
    """When two modules emit the same technique_id, first writer's metadata wins."""
    m1 = _ConstModule("m1", [_hit("tid-dup", name="Original", confidence=0.8)])
    m2 = _ConstModule("m2", [_hit("tid-dup", name="Duplicate", confidence=0.7)])
    pipeline = AnalysisPipeline(modules=[m1, m2])
    hits = await pipeline.run(_ctx())
    assert len(hits) == 1
    assert hits[0].name == "Original"


@pytest.mark.asyncio
async def test_pipeline_deduplicate_highest_confidence_kept() -> None:
    """When two modules emit the same technique_id, highest confidence wins."""
    m1 = _ConstModule("m1", [_hit("tid-dup", confidence=0.6)])
    m2 = _ConstModule("m2", [_hit("tid-dup", confidence=0.95)])
    pipeline = AnalysisPipeline(modules=[m1, m2])
    hits = await pipeline.run(_ctx())
    assert len(hits) == 1
    assert hits[0].confidence == 0.95


@pytest.mark.asyncio
async def test_pipeline_all_raising_returns_empty() -> None:
    pipeline = AnalysisPipeline(modules=[_RaisingModule(), _RaisingModule()])
    hits = await pipeline.run(_ctx())
    assert hits == []


@pytest.mark.asyncio
async def test_pipeline_single_module_no_hits() -> None:
    pipeline = AnalysisPipeline(modules=[_ConstModule("empty", [])])
    hits = await pipeline.run(_ctx())
    assert hits == []
