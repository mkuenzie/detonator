"""Run lifecycle state machine.

Drives a single detonation end-to-end:

    pending
      → provisioning   (revert VM to clean snapshot)
      → preflight      (verify egress / isolation — stubbed in phase 2)
      → detonating     (start VM, wait for agent, trigger detonation)
      → [interactive]  (paused for analyst takeover, awaiting /resume)
      → collecting     (download artifacts from agent, stop VM)
      → enriching      (stubbed in phase 2 — real work lands in phase 4)
      → filtering      (stubbed in phase 2 — real work lands in phase 5)
      → complete | error

Each stage logs a ``StateTransition`` with a timestamp + detail. Errors at
any stage move the run to ``error`` with whatever artifacts have already
been captured preserved on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from detonator.analysis.navigation import extract_navigation_scope
from detonator.analysis.filter import NoiseFilter
from detonator.analysis.har_body_map import HarBodyRef, load_capture_manifest, map_body_files
from detonator.analysis.modules.base import AnalysisContext
from detonator.analysis.modules.pipeline import AnalysisPipeline
from detonator.config import AgentInstanceConfig, DetonatorConfig
from detonator.enrichment.pipeline import EnrichmentPipeline
from detonator.logging import RunAdapter
from detonator.models import ArtifactType, RunConfig, RunRecord, RunState, StateTransition
from detonator.orchestrator.agent_manager import AgentManager
from detonator.providers.egress.base import EgressProvider
from detonator.providers.vm.base import VMProvider
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore
from detonator.storage.manifest import build_manifest

logger = logging.getLogger(__name__)


class RunnerError(Exception):
    """Raised when a stage fails fatally."""


def _now() -> datetime:
    return datetime.now(UTC)


class Runner:
    """Executes a single detonation run through the state machine.

    One ``Runner`` instance handles one run. Create a new one per submission.
    The orchestrator holds references so it can dispatch control operations
    (e.g. ``resume``) into an in-flight run.
    """

    def __init__(
        self,
        *,
        config: DetonatorConfig,
        agent: AgentInstanceConfig,
        vm_provider: VMProvider,
        database: Database,
        artifact_store: ArtifactStore,
        run_config: RunConfig,
        run_id: UUID | None = None,
        egress_provider: EgressProvider | None = None,
        enrichment_pipeline: EnrichmentPipeline | None = None,
        analysis_pipeline: AnalysisPipeline | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.vm_provider = vm_provider
        self.database = database
        self.artifact_store = artifact_store
        self.egress_provider = egress_provider
        self.enrichment_pipeline = enrichment_pipeline
        self.analysis_pipeline = analysis_pipeline
        self.record = RunRecord(
            id=run_id or uuid4(),
            config=run_config,
        )
        self._resume_event = asyncio.Event()
        # Per-run structured logger — injects run_id into every record.
        self._log = RunAdapter(logger, str(self.record.id))

    @property
    def run_id(self) -> UUID:
        return self.record.id

    # ── State transitions ────────────────────────────────────────

    async def _transition(self, to_state: RunState, detail: str | None = None) -> None:
        from_state = self.record.state
        self.record.state = to_state
        self.record.transitions.append(
            StateTransition(from_state=from_state, to_state=to_state, detail=detail)
        )
        self._log.info(
            "%s → %s%s",
            from_state.value,
            to_state.value,
            f" ({detail})" if detail else "",
        )
        await self.database.update_run_status(
            str(self.record.id),
            to_state.value,
            error=self.record.error,
        )

    def signal_resume(self) -> None:
        """Called by the API when the analyst wants to end an interactive pause."""
        self._resume_event.set()

    # ── Persistence helpers ──────────────────────────────────────

    async def _persist_initial_record(self) -> None:
        self.record.artifact_dir = str(self.artifact_store.ensure_run_dir(str(self.record.id)))
        await self.database.insert_run(
            run_id=str(self.record.id),
            seed_url=self.record.config.url,
            egress_type=self.record.config.egress.value,
            config=self.record.config.model_dump(),
            created_at=self.record.created_at.isoformat(),
        )

    async def _write_meta(self) -> None:
        """Dump the run record to ``meta.json`` inside the artifact dir."""
        meta = self.record.model_dump(mode="json")
        meta_bytes = json.dumps(meta, indent=2, default=str).encode("utf-8")
        path, size, content_hash = self.artifact_store.store_bytes(
            str(self.record.id), "meta.json", meta_bytes
        )
        await self.database.insert_artifact(
            str(self.record.id),
            ArtifactType.META,
            str(path),
            size=size,
            content_hash=content_hash,
        )

    async def _write_manifest(self) -> None:
        """Assemble and persist ``manifest.json`` — consolidated run summary."""
        artifact_dir = self.record.artifact_dir
        if not artifact_dir:
            return

        try:
            run_row = await self.database.get_run(str(self.record.id)) or {}
            artifacts = await self.database.get_artifacts(str(self.record.id))
            technique_matches = await self.database.get_technique_matches_for_run(
                str(self.record.id)
            )
            manifest = build_manifest(
                run_id=str(self.record.id),
                run_row=run_row,
                artifacts=artifacts,
                technique_matches=technique_matches,
                artifact_dir=self.artifact_store.run_dir(str(self.record.id)),
            )
            manifest_bytes = json.dumps(manifest, indent=2, default=str).encode("utf-8")
            path, size, content_hash = self.artifact_store.store_bytes(
                str(self.record.id), "manifest.json", manifest_bytes
            )
            await self.database.insert_artifact(
                str(self.record.id),
                ArtifactType.MANIFEST,
                str(path),
                size=size,
                content_hash=content_hash,
            )
            self._log.info("manifest.json written (%d bytes)", size)
        except Exception as exc:
            # Non-fatal: manifest failure must never suppress the run result.
            self._log.warning("failed to write manifest.json: %s", exc)

    # ── Main entrypoint ──────────────────────────────────────────

    async def execute(self) -> RunRecord:
        """Run the full lifecycle. Returns the finished ``RunRecord``."""
        await self._persist_initial_record()

        try:
            try:
                await self._provision()
                await self._preflight()
                await self._detonate_and_collect()
                await self._enrich()
                await self._filter()
                await self._complete()
            except asyncio.CancelledError:
                await self._fail("cancelled")
                raise
            except Exception as exc:
                self._log.exception("run failed")
                await self._fail(str(exc))
        finally:
            await self._teardown_egress()

        await self._write_manifest()
        await self._write_meta()
        return self.record

    # ── Stages ───────────────────────────────────────────────────

    async def _provision(self) -> None:
        """Revert the VM to its clean snapshot and start it."""
        vm_id = self.record.config.vm_id or self.agent.vm_id
        snapshot = self.record.config.snapshot_id or self.agent.snapshot
        if not vm_id or not snapshot:
            raise RunnerError("vm_id and snapshot_id must be set (via agent config or request)")

        await self._transition(RunState.PROVISIONING, f"vm={vm_id} snapshot={snapshot}")

        async with asyncio.timeout(self.config.timeouts.provision_sec):
            await self.vm_provider.revert(vm_id, snapshot)
            await self.vm_provider.start(vm_id)

    async def _preflight(self) -> None:
        """Activate egress and verify isolation."""
        if self.egress_provider is None:
            await self._transition(RunState.PREFLIGHT, "no egress provider configured")
            return

        vm_id = self.record.config.vm_id or self.agent.vm_id
        await self._transition(RunState.PREFLIGHT, f"activating egress for vm={vm_id}")

        async with asyncio.timeout(self.config.timeouts.preflight_sec):
            await self.egress_provider.activate(vm_id)
            result = await self.egress_provider.preflight_check(vm_id)

        if not result.passed:
            detail = "; ".join(result.details) if result.details else "preflight failed"
            raise RunnerError(f"Egress preflight failed: {detail}")

        self._log.info("preflight passed: public_ip=%s", result.public_ip)

    async def _teardown_egress(self) -> None:
        """Deactivate the egress provider. Always called; errors are non-fatal."""
        if self.egress_provider is None:
            return
        vm_id = self.record.config.vm_id or self.agent.vm_id
        try:
            await self.egress_provider.deactivate(vm_id)
        except Exception as exc:
            self._log.warning("egress deactivate failed: %s", exc)

    async def _wait_for_ip(self, vm_id: str) -> str:
        """Poll get_network_info until the guest agent reports an IP address.

        The QEMU guest agent starts after the OS boots, which on Windows can
        take 30–90 seconds.  We reuse the agent health-check timeout budget
        since both waits are gating the same thing: the VM being ready.
        """
        timeout = self.agent.health_timeout_sec
        poll = self.agent.health_poll_sec
        elapsed = 0.0
        while elapsed < timeout:
            net_info = await self.vm_provider.get_network_info(vm_id)
            if net_info.ip_address:
                return net_info.ip_address
            self._log.debug(
                "VM %s has no IP yet — waiting for guest agent (%.0fs/%.0fs)",
                vm_id,
                elapsed,
                timeout,
            )
            await asyncio.sleep(poll)
            elapsed += poll
        raise RunnerError(
            f"VM {vm_id} did not report an IP within {timeout}s — guest agent not ready?"
        )

    async def _detonate_and_collect(self) -> None:
        """Trigger the in-VM agent and collect artifacts."""
        await self._transition(RunState.DETONATING)

        vm_id = self.record.config.vm_id or self.agent.vm_id
        assert vm_id is not None  # already validated in _provision

        # Wait for the guest agent to report the VM's IP (Windows takes time to boot).
        ip = await self._wait_for_ip(vm_id)
        base_url = f"http://{ip}:{self.agent.port}"
        self._log.info("agent base_url=%s", base_url)

        async with AgentManager(base_url) as agent:
            await agent.wait_for_health(
                timeout_sec=self.agent.health_timeout_sec,
                poll_sec=self.agent.health_poll_sec,
            )

            await agent.detonate(
                url=self.record.config.url,
                timeout_sec=self.record.config.timeout_sec,
                interactive=self.record.config.interactive,
                screenshot_interval_sec=self.record.config.screenshot_interval_sec,
                stealth=self.agent.stealth,
            )

            if self.record.config.interactive:
                await self._handle_interactive_pause(agent)

            async with asyncio.timeout(self.config.timeouts.detonate_sec):
                final = await agent.wait_for_terminal(
                    timeout_sec=self.config.timeouts.detonate_sec,
                    poll_sec=2.0,
                )

            if final.state == "error":
                raise RunnerError(f"agent reported error: {final.error}")

            await self._transition(RunState.COLLECTING)
            async with asyncio.timeout(self.config.timeouts.collect_sec):
                await self._collect_artifacts(agent)

        # Stop the VM after artifacts are safely on disk.
        try:
            await self.vm_provider.stop(vm_id, force=True)
        except Exception as exc:  # non-fatal — artifacts already saved
            self._log.warning("VM stop failed: %s", exc)

    async def _handle_interactive_pause(self, agent: AgentManager) -> None:
        """Wait for the agent to enter `paused`, then block until resume is signaled."""
        # Poll until the agent says it's paused (analyst takeover ready).
        # No timeout — the analyst controls pacing in interactive mode.
        st = await agent.wait_for_terminal(
            timeout_sec=None,
            poll_sec=2.0,
            pause_on_interactive=True,
        )
        if st.state != "paused":
            # Detonation finished or errored before reaching the interactive hold.
            return

        await self._transition(RunState.INTERACTIVE, "awaiting analyst resume")
        await self._resume_event.wait()
        await agent.resume()
        await self._transition(RunState.DETONATING, "resumed from interactive")

    async def _collect_artifacts(self, agent: AgentManager) -> None:
        """Download all artifacts from the agent and persist them + index them in SQLite."""
        from pathlib import Path

        run_dir = self.artifact_store.ensure_run_dir(str(self.record.id))
        downloaded = await agent.download_all(run_dir)

        # Parse the HAR once so the insert loop can classify + stamp body files
        # with their originating URL in a single pass. Merge in the agent's
        # bodies/extra.json sidecar for responses Playwright's HAR writer
        # dropped (main-frame docs, some POST responses) — HAR wins on
        # conflict since it's the canonical evidence record.
        body_map: dict[str, HarBodyRef] = {}
        har_path = run_dir / "har_full.har"
        if har_path.exists():
            try:
                body_map = map_body_files(har_path)
            except Exception as exc:
                self._log.warning("har_body_map failed: %s", exc)
        try:
            for basename, ref in load_capture_manifest(run_dir).items():
                body_map.setdefault(basename, ref)
        except Exception as exc:
            self._log.warning("load_capture_manifest failed: %s", exc)

        for name, path, size in downloaded:
            ref = body_map.get(Path(name).name)
            artifact_type = self._infer_artifact_type(name, body_ref=ref)
            symlink_path, size, content_hash = self.artifact_store.adopt(
                str(self.record.id), name, path
            )
            await self.database.insert_artifact(
                str(self.record.id),
                artifact_type,
                str(symlink_path),
                size=size,
                content_hash=content_hash,
                source_url=(ref.url if ref else None),
            )

    @staticmethod
    def _infer_artifact_type(
        name: str, *, body_ref: HarBodyRef | None = None
    ) -> ArtifactType:
        """Map an artifact filename to an ``ArtifactType``.

        When ``body_ref`` is set, the file is a Playwright HAR body attachment
        and the HAR direction drives classification:

        - ``response`` → ``SITE_RESOURCE`` (a resource the site loaded)
        - ``request``  → ``REQUEST_BODY``  (an outgoing POST/PUT payload —
          telemetry beacons, form submissions, fingerprint uploads)

        With no body_ref, fall back to filename heuristics for the files the
        agent writes directly (HAR, DOM, console, screenshots, meta, …).
        """
        if body_ref is not None:
            if body_ref.direction == "request":
                return ArtifactType.REQUEST_BODY
            return ArtifactType.SITE_RESOURCE
        lower = name.lower()
        if lower.startswith("screenshots/") or lower.endswith((".png", ".jpg", ".jpeg")):
            return ArtifactType.SCREENSHOT
        if lower.endswith("navigations.json"):
            return ArtifactType.NAVIGATIONS
        if "har" in lower and lower.endswith((".har", ".json")):
            return ArtifactType.HAR_FULL
        if lower.endswith("dom.html") or lower.endswith(".html"):
            return ArtifactType.DOM
        if "console" in lower:
            return ArtifactType.CONSOLE
        return ArtifactType.META

    async def _enrich(self) -> None:
        """Run the enrichment pipeline against collected artifacts."""
        if self.enrichment_pipeline is None:
            await self._transition(RunState.ENRICHING, "no pipeline configured")
            return

        artifact_dir = self.record.artifact_dir
        if not artifact_dir:
            await self._transition(RunState.ENRICHING, "no artifact_dir — skipping")
            return

        await self._transition(RunState.ENRICHING)
        async with asyncio.timeout(self.config.timeouts.enrich_sec):
            results = await self.enrichment_pipeline.run(
                str(self.record.id),
                artifact_dir,
                self.record.config.url,
            )

        errors = [r for r in results if r.error]
        obs_count = sum(len(r.observables) for r in results)
        detail = f"{len(results)} enricher results, {obs_count} observables"
        if errors:
            detail += f", {len(errors)} errors"
        self._log.info("enrichment complete: %s", detail)

    async def _filter(self) -> None:
        """Extract the navigation scope, classify noise, detect techniques."""
        artifact_dir = self.record.artifact_dir
        if not artifact_dir:
            await self._transition(RunState.FILTERING, "no artifact_dir — skipping")
            return

        from pathlib import Path

        har_path = Path(artifact_dir) / "har_full.har"
        if not har_path.exists():
            await self._transition(RunState.FILTERING, "no har_full.har — skipping")
            return

        await self._transition(RunState.FILTERING)

        navigations_path = Path(artifact_dir) / "navigations.json"

        async with asyncio.timeout(self.config.timeouts.filter_sec):
            nav_scope = extract_navigation_scope(
                har_path,
                navigations_path if navigations_path.exists() else None,
                self.record.config.url,
            )

        if nav_scope is None:
            self._log.warning("navigation-scope extraction returned no result")
            return

        noise_filter = NoiseFilter(
            noise_domains=self.config.filter.noise_domains,
            noise_resource_types=self.config.filter.noise_resource_types,
            require_navigation_scope=self.config.filter.require_initiator_chain,
        )
        filter_result = noise_filter.run(nav_scope, str(self.record.id))

        # Write har_navigation.json
        import json as _json

        har_nav_bytes = _json.dumps(filter_result.har_navigation, indent=2).encode("utf-8")
        nav_path, nav_size, nav_hash = self.artifact_store.store_bytes(
            str(self.record.id), "har_navigation.json", har_nav_bytes
        )
        await self.database.insert_artifact(
            str(self.record.id),
            ArtifactType.HAR_NAVIGATION,
            str(nav_path),
            size=nav_size,
            content_hash=nav_hash,
        )

        # Write filter_result.json for analyst inspection
        filter_bytes = _json.dumps(filter_result.model_dump(mode="json"), indent=2).encode("utf-8")
        fr_path, fr_size, fr_hash = self.artifact_store.store_bytes(
            str(self.record.id), "filter_result.json", filter_bytes
        )
        await self.database.insert_artifact(
            str(self.record.id),
            ArtifactType.FILTER_RESULT,
            str(fr_path),
            size=fr_size,
            content_hash=fr_hash,
        )

        # Run analysis pipeline against the noise-filtered navigation scope
        technique_hits = []
        if self.analysis_pipeline is not None:
            artifacts = await self.database.get_artifacts(str(self.record.id))
            ctx = AnalysisContext.from_navigation_scope(
                nav_scope,
                filter_result,
                artifact_dir,
                str(self.record.id),
                self.record.config.url,
                artifacts=artifacts,
            )
            technique_hits = await self.analysis_pipeline.run(ctx)

        for hit in technique_hits:
            await self.database.upsert_technique(
                tech_id=hit.technique_id,
                name=hit.name,
                description=hit.description,
                signature_type=hit.signature_type,
                detection_module=hit.detection_module,
            )
            await self.database.insert_technique_match(
                technique_id=hit.technique_id,
                run_id=str(self.record.id),
                confidence=hit.confidence,
                evidence=hit.evidence,
            )

        detail = (
            f"scope={filter_result.scope_requests} "
            f"noise={filter_result.noise_requests} "
            f"techniques={len(technique_hits)}"
        )
        self._log.info("filter complete: %s", detail)

    async def _complete(self) -> None:
        self.record.completed_at = _now()
        await self._transition(RunState.COMPLETE)
        await self.database.update_run_status(
            str(self.record.id),
            RunState.COMPLETE.value,
            completed_at=self.record.completed_at.isoformat(),
        )

    async def _fail(self, reason: str) -> None:
        self.record.error = reason
        self.record.completed_at = _now()
        await self._transition(RunState.ERROR, reason)
        await self.database.update_run_status(
            str(self.record.id),
            RunState.ERROR.value,
            completed_at=self.record.completed_at.isoformat(),
            error=reason,
        )
