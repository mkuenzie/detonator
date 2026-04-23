"""Enrichment pipeline — fans out to all registered enrichers and persists results.

Usage
-----
Build the pipeline once at app startup::

    pipeline = EnrichmentPipeline.build_from_config(config, db, artifact_store)

Then call it from the runner's ``_enrich()`` stage::

    results = await pipeline.run(run_id, artifact_dir, seed_url)

The pipeline:

1. Scans *artifact_dir* for a ``har_full.har`` to extract domains / IPs / URLs.
2. Also checks for ``dom.html`` so the DOM extractor can run.
3. Determines which artifact types are available and fans out to all enrichers
   whose ``accepts()`` returns True for any available type.
4. Runs all accepted enrichers concurrently (``asyncio.gather`` with
   ``return_exceptions=True`` — a failing enricher never aborts the pipeline).
5. Persists the raw results to ``enrichment.json`` in *artifact_dir*.
6. Upserts observables and observable links into SQLite, then links each
   observable back to the run via ``run_observables``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from detonator.config import DetonatorConfig
from detonator.enrichment.base import Enricher, EnrichmentResult, RunContext
from detonator.enrichment.har import extract_from_har
from detonator.models.observables import Observable, ObservableLink, ObservableSource, ObservableType
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore

logger = logging.getLogger(__name__)


class EnrichmentPipeline:
    """Orchestrates concurrent enrichment and persists all results."""

    def __init__(
        self,
        enrichers: list[Enricher],
        database: Database,
        artifact_store: ArtifactStore,
    ) -> None:
        self._enrichers = enrichers
        self._db = database
        self._artifact_store = artifact_store

    # ── Public entry point ────────────────────────────────────────

    async def run(self, run_id: str, artifact_dir: str, seed_url: str) -> list[EnrichmentResult]:
        """Execute the full enrichment pipeline for one run.

        Returns all ``EnrichmentResult`` instances (including error results).
        Results are also persisted to *artifact_dir/enrichment.json* and
        observables are upserted into the database.
        """
        context = self._build_context(run_id, artifact_dir, seed_url)
        available_types = self._available_artifact_types(Path(artifact_dir))

        # Fan out to all enrichers that accept at least one available artifact type.
        accepted = [e for e in self._enrichers if any(e.accepts(t) for t in available_types)]
        if not accepted:
            logger.info("run=%s no enrichers accepted available artifact types %s", run_id, available_types)
            return []

        logger.info(
            "run=%s running %d enrichers: %s",
            run_id,
            len(accepted),
            [e.name for e in accepted],
        )

        # Load per-module exclusions fresh from DB so UI edits take effect immediately.
        exclusions = await self._db.list_enrichment_exclusions()
        for enricher in accepted:
            enricher._exclude_hosts = {h.lower() for h in exclusions.get(enricher.name, set())}

        raw = await asyncio.gather(
            *[self._run_enricher(e, context) for e in accepted],
            return_exceptions=True,
        )

        all_results: list[EnrichmentResult] = []
        for enricher, outcome in zip(accepted, raw):
            if isinstance(outcome, Exception):
                logger.error("run=%s enricher=%s raised: %s", run_id, enricher.name, outcome)
                all_results.append(
                    EnrichmentResult(
                        enricher=enricher.name,
                        input_value="",
                        error=f"Enricher raised an exception: {outcome}",
                    )
                )
            else:
                all_results.extend(outcome)

        await self._persist(run_id, artifact_dir, seed_url, all_results)
        return all_results

    # ── Internal helpers ──────────────────────────────────────────

    async def _run_enricher(
        self, enricher: Enricher, context: RunContext
    ) -> list[EnrichmentResult]:
        """Run a single enricher, catching all exceptions."""
        try:
            return await enricher.enrich(context)
        except Exception as exc:
            logger.error("run=%s enricher=%s unhandled exception: %s", context.run_id, enricher.name, exc)
            raise

    def _build_context(self, run_id: str, artifact_dir: str, seed_url: str) -> RunContext:
        """Populate RunContext from HAR artifacts if available."""
        domains: list[str] = []
        ips: list[str] = []
        urls: list[str] = []

        har_path = Path(artifact_dir) / "har_full.har"
        if har_path.exists():
            domains, ips, urls = extract_from_har(har_path)
            logger.info(
                "run=%s HAR extracted: %d domains, %d IPs, %d URLs",
                run_id, len(domains), len(ips), len(urls),
            )
        else:
            logger.debug("run=%s har_full.har not found, context will have empty domain/URL lists", run_id)

        return RunContext(
            run_id=run_id,
            artifact_dir=artifact_dir,
            seed_url=seed_url,
            domains=domains,
            ips=ips,
            urls=urls,
        )

    @staticmethod
    def _available_artifact_types(artifact_dir: Path) -> list[str]:
        """Return the list of artifact types present in the run directory."""
        types: list[str] = []
        if (artifact_dir / "har_full.har").exists():
            types.extend(["har", "domain", "ip", "url"])
        if (artifact_dir / "dom.html").exists():
            types.append("dom")
        if (artifact_dir / "navigations.json").exists():
            types.append("navigations")
        return types

    async def _persist(
        self,
        run_id: str,
        artifact_dir: str,
        seed_url: str,
        results: list[EnrichmentResult],
    ) -> None:
        """Write enrichment.json and upsert all observables + links into SQLite."""
        await self._write_enrichment_json(run_id, artifact_dir, results)

        now = datetime.now(UTC).isoformat()

        # Collect all unique observables (deduplicate by id), tracking which enricher
        # produced each one so context_json carries the real module name.
        seen_obs_ids: set[str] = set()
        observables: list[tuple[Observable, str]] = []
        for r in results:
            for obs in r.observables:
                key = str(obs.id)
                if key not in seen_obs_ids:
                    seen_obs_ids.add(key)
                    observables.append((obs, r.enricher))

        # Collect all links (deduplicate by source+target+relationship).
        seen_link_keys: set[str] = set()
        links: list[ObservableLink] = []
        for r in results:
            for link in r.observable_links:
                key = f"{link.source_id}:{link.target_id}:{link.relationship}"
                if key not in seen_link_keys:
                    seen_link_keys.add(key)
                    links.append(link)

        # Also ensure the seed domain itself is an observable.
        from urllib.parse import urlparse
        seed_host = urlparse(seed_url).hostname or ""
        if seed_host:
            seed_obs_id = str(_domain_obs_id(seed_host))
            if seed_obs_id not in seen_obs_ids:
                seen_obs_ids.add(seed_obs_id)
                from detonator.enrichment.base import observable_id
                observables.insert(
                    0,
                    (
                        Observable(
                            id=observable_id(ObservableType.DOMAIN, seed_host),
                            type=ObservableType.DOMAIN,
                            value=seed_host,
                            first_seen=datetime.now(UTC),
                            last_seen=datetime.now(UTC),
                        ),
                        "pipeline",
                    ),
                )

        # Upsert observables.
        for obs, enricher_name in observables:
            await self._db.upsert_observable(
                str(obs.id), obs.type.value, obs.value, now
            )
            if obs.metadata:
                await self._db.upsert_observable_metadata(str(obs.id), obs.metadata)
            await self._db.link_run_observable(
                run_id,
                str(obs.id),
                ObservableSource.ENRICHMENT.value,
                context={"enricher": enricher_name},
            )

        # Upsert observable→observable links.
        for link in links:
            # Ensure both endpoints are in the DB before creating the link.
            await self._safe_link_observables(link, now)

        logger.info(
            "run=%s enrichment persisted: %d observables, %d links",
            run_id, len(observables), len(links),
        )

    async def _safe_link_observables(self, link: ObservableLink, seen_at: str) -> None:
        try:
            await self._db.link_observables(
                str(link.source_id),
                str(link.target_id),
                link.relationship.value,
                seen_at,
                confidence=link.confidence,
                evidence=link.evidence,
            )
        except Exception as exc:
            # Missing foreign key (endpoint not yet in DB) — log and continue.
            logger.warning("Could not persist observable link %s→%s: %s", link.source_id, link.target_id, exc)

    async def _write_enrichment_json(
        self, run_id: str, artifact_dir: str, results: list[EnrichmentResult]
    ) -> None:
        """Write enrichment.json to the artifact directory."""
        out_path = Path(artifact_dir) / "enrichment.json"
        payload = {
            "run_id": run_id,
            "enriched_at": datetime.now(UTC).isoformat(),
            "results": [r.model_dump(mode="json") for r in results],
        }
        try:
            out_path.write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            logger.debug("run=%s wrote enrichment.json (%d bytes)", run_id, out_path.stat().st_size)
        except OSError as exc:
            logger.error("run=%s could not write enrichment.json: %s", run_id, exc)

    # ── Factory ───────────────────────────────────────────────────

    @classmethod
    def build_from_config(
        cls,
        config: DetonatorConfig,
        database: Database,
        artifact_store: ArtifactStore,
    ) -> EnrichmentPipeline:
        """Instantiate an ``EnrichmentPipeline`` from ``config.enrichment.modules``.

        Core enrichers (navigations, dom) always run. Plug-in enrichers are
        enabled by listing their short names in config.enrichment.modules.
        Unknown module names are logged and skipped so a typo in config doesn't
        crash the orchestrator at startup. If a name matches a core enricher it
        is silently ignored (core enrichers are unconditional).
        """
        from detonator.enrichment.core import CORE_ENRICHERS
        from detonator.enrichment.plugins import PLUGIN_ENRICHERS

        core_enrichers: list[Enricher] = [cls_() for cls_ in CORE_ENRICHERS.values()]

        plugin_enrichers: list[Enricher] = []
        for module_name in config.enrichment.modules:
            if module_name in CORE_ENRICHERS:
                logger.info("Enrichment module %r is now a core enricher and always runs — ignoring config entry", module_name)
                continue
            if module_name in PLUGIN_ENRICHERS:
                plugin_enrichers.append(PLUGIN_ENRICHERS[module_name]())
            else:
                logger.warning("Unknown enrichment module %r — skipping", module_name)

        enrichers = core_enrichers + plugin_enrichers
        logger.info(
            "EnrichmentPipeline built — Core: %s | Plug-ins enabled: %s",
            [e.name for e in core_enrichers],
            [e.name for e in plugin_enrichers],
        )
        return cls(enrichers=enrichers, database=database, artifact_store=artifact_store)


def _domain_obs_id(domain: str) -> object:
    from detonator.enrichment.base import observable_id
    return observable_id(ObservableType.DOMAIN, domain)
