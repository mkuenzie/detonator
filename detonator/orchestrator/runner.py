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

from detonator.config import DetonatorConfig
from detonator.models import RunConfig, RunRecord, RunState, StateTransition
from detonator.orchestrator.agent_manager import AgentManager
from detonator.providers.vm.base import VMProvider
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore

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
        vm_provider: VMProvider,
        database: Database,
        artifact_store: ArtifactStore,
        run_config: RunConfig,
        run_id: UUID | None = None,
    ) -> None:
        self.config = config
        self.vm_provider = vm_provider
        self.database = database
        self.artifact_store = artifact_store
        self.record = RunRecord(
            id=run_id or uuid4(),
            config=run_config,
        )
        self._resume_event = asyncio.Event()

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
        logger.info(
            "run=%s %s → %s%s",
            self.record.id,
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
            str(self.record.id), "meta", str(path), size=size, content_hash=content_hash
        )

    # ── Main entrypoint ──────────────────────────────────────────

    async def execute(self) -> RunRecord:
        """Run the full lifecycle. Returns the finished ``RunRecord``."""
        await self._persist_initial_record()

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
            logger.exception("run=%s failed", self.record.id)
            await self._fail(str(exc))

        await self._write_meta()
        return self.record

    # ── Stages ───────────────────────────────────────────────────

    async def _provision(self) -> None:
        """Revert the VM to its clean snapshot and start it."""
        vm_id = self.record.config.vm_id or self.config.default_vm_id
        snapshot = self.record.config.snapshot_id or self.config.default_snapshot
        if not vm_id or not snapshot:
            raise RunnerError("vm_id and snapshot_id must be set (via config or request)")

        await self._transition(RunState.PROVISIONING, f"vm={vm_id} snapshot={snapshot}")

        async with asyncio.timeout(self.config.timeouts.provision_sec):
            await self.vm_provider.revert(vm_id, snapshot)
            await self.vm_provider.start(vm_id)

    async def _preflight(self) -> None:
        """Verify isolation + egress. Stubbed in phase 2 — becomes real in phase 3."""
        await self._transition(RunState.PREFLIGHT, "skipped (phase 3)")
        # Intentional no-op in phase 2. Phase 3 will plug in EgressProvider.preflight_check().

    async def _wait_for_ip(self, vm_id: str) -> str:
        """Poll get_network_info until the guest agent reports an IP address.

        The QEMU guest agent starts after the OS boots, which on Windows can
        take 30–90 seconds.  We reuse the agent health-check timeout budget
        since both waits are gating the same thing: the VM being ready.
        """
        timeout = self.config.agent.health_timeout_sec
        poll = self.config.agent.health_poll_sec
        elapsed = 0.0
        while elapsed < timeout:
            net_info = await self.vm_provider.get_network_info(vm_id)
            if net_info.ip_address:
                return net_info.ip_address
            logger.debug(
                "run=%s VM %s has no IP yet — waiting for guest agent (%.0fs/%.0fs)",
                self.record.id,
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

        vm_id = self.record.config.vm_id or self.config.default_vm_id
        assert vm_id is not None  # already validated in _provision

        # Wait for the guest agent to report the VM's IP (Windows takes time to boot).
        ip = await self._wait_for_ip(vm_id)
        base_url = f"http://{ip}:{self.config.agent.port}"
        logger.info("run=%s agent base_url=%s", self.record.id, base_url)

        async with AgentManager(base_url) as agent:
            await agent.wait_for_health(
                timeout_sec=self.config.agent.health_timeout_sec,
                poll_sec=self.config.agent.health_poll_sec,
            )

            await agent.detonate(
                url=self.record.config.url,
                timeout_sec=self.record.config.timeout_sec,
                interactive=self.record.config.interactive,
                screenshot_interval_sec=self.record.config.screenshot_interval_sec,
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
            logger.warning("run=%s VM stop failed: %s", self.record.id, exc)

    async def _handle_interactive_pause(self, agent: AgentManager) -> None:
        """Wait for the agent to enter `paused`, then block until resume is signaled."""
        # First, poll until the agent says it's paused (analyst takeover ready).
        st = await agent.wait_for_terminal(
            timeout_sec=self.config.timeouts.detonate_sec,
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
        run_dir = self.artifact_store.ensure_run_dir(str(self.record.id))
        downloaded = await agent.download_all(run_dir)

        for name, path, size in downloaded:
            artifact_type = self._infer_artifact_type(name)
            # ArtifactStore.download path is already final; compute hash now.
            content_hash = ArtifactStore._sha256(path)
            await self.database.insert_artifact(
                str(self.record.id),
                artifact_type,
                str(path),
                size=size,
                content_hash=content_hash,
            )

    @staticmethod
    def _infer_artifact_type(name: str) -> str:
        """Map an artifact filename to one of our ``ArtifactType`` values."""
        lower = name.lower()
        if lower.startswith("screenshots/") or lower.endswith((".png", ".jpg", ".jpeg")):
            return "screenshot"
        if "har" in lower and lower.endswith(".json"):
            return "har_full"
        if lower.endswith("dom.html") or lower.endswith(".html"):
            return "dom"
        if "console" in lower:
            return "console"
        if "meta" in lower:
            return "meta"
        return "meta"  # fallback bucket

    async def _enrich(self) -> None:
        """Stub — phase 4 wires the enrichment pipeline here."""
        await self._transition(RunState.ENRICHING, "skipped (phase 4)")

    async def _filter(self) -> None:
        """Stub — phase 5 wires the chain extractor here."""
        await self._transition(RunState.FILTERING, "skipped (phase 5)")

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
