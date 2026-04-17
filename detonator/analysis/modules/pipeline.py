"""Analysis pipeline — fans out to all registered AnalysisModules.

Usage
-----
Build the pipeline once at app startup::

    pipeline = AnalysisPipeline.build_from_config(config)

Then call it from the runner's ``_filter()`` stage::

    hits = await pipeline.run(context)

The pipeline:

1. Fans out to all registered modules concurrently.
2. Swallows per-module exceptions (a failing module never aborts the pipeline).
3. Aggregates hits, deduplicating by ``technique_id`` — first writer wins for
   metadata; highest confidence value is kept.

Persistence is NOT this pipeline's job — it returns hits and the runner
persists them, mirroring the separation already established for enrichment.
"""

from __future__ import annotations

import asyncio
import logging

from detonator.analysis.modules.base import AnalysisContext, AnalysisModule, TechniqueHit
from detonator.config import DetonatorConfig

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    """Orchestrates concurrent analysis across registered modules."""

    def __init__(self, modules: list[AnalysisModule]) -> None:
        self._modules = modules

    async def run(self, context: AnalysisContext) -> list[TechniqueHit]:
        """Execute all modules concurrently and return deduplicated hits."""
        if not self._modules:
            logger.info("run=%s analysis: no modules configured", context.run_id)
            return []

        logger.info(
            "run=%s analysis: running %d module(s): %s",
            context.run_id,
            len(self._modules),
            [m.name for m in self._modules],
        )

        raw = await asyncio.gather(
            *[self._run_module(m, context) for m in self._modules],
            return_exceptions=True,
        )

        all_hits: list[TechniqueHit] = []
        for module, outcome in zip(self._modules, raw):
            if isinstance(outcome, Exception):
                logger.error(
                    "run=%s analysis module=%s raised: %s",
                    context.run_id,
                    module.name,
                    outcome,
                )
            else:
                all_hits.extend(outcome)

        return _dedupe(all_hits)

    async def _run_module(
        self, module: AnalysisModule, context: AnalysisContext
    ) -> list[TechniqueHit]:
        try:
            return await module.analyze(context)
        except Exception as exc:
            logger.error(
                "run=%s analysis module=%s unhandled exception: %s",
                context.run_id,
                module.name,
                exc,
            )
            raise

    @classmethod
    def build_from_config(cls, config: DetonatorConfig) -> AnalysisPipeline:
        """Instantiate an ``AnalysisPipeline`` from ``config.analysis.modules``.

        Unknown module names are logged and skipped.
        """
        modules: list[AnalysisModule] = []

        for module_name in config.analysis.modules:
            module = _build_module(module_name, config)
            if module is not None:
                modules.append(module)
            else:
                logger.warning("Unknown analysis module %r — skipping", module_name)

        logger.info(
            "AnalysisPipeline built with %d module(s): %s",
            len(modules),
            [m.name for m in modules],
        )
        return cls(modules=modules)


def _build_module(name: str, config: DetonatorConfig) -> AnalysisModule | None:
    """Instantiate a single analysis module by name. Returns None if unrecognised."""
    if name == "sigma":
        from detonator.analysis.modules.sigma import SigmaModule
        return SigmaModule(rules_dirs=config.analysis.rules_dirs)
    return None


def _dedupe(hits: list[TechniqueHit]) -> list[TechniqueHit]:
    """Deduplicate by technique_id — first writer wins for metadata, highest confidence kept."""
    seen: dict[str, TechniqueHit] = {}
    for hit in hits:
        if hit.technique_id not in seen:
            seen[hit.technique_id] = hit
        else:
            existing = seen[hit.technique_id]
            if hit.confidence > existing.confidence:
                # Keep existing metadata but upgrade confidence
                seen[hit.technique_id] = existing.model_copy(update={"confidence": hit.confidence})
    return list(seen.values())
