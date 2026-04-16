"""Shared application state for the orchestrator.

Holds the long-lived singletons (config, VM provider, database, artifact
store) and the registry of in-flight runs so API handlers can deliver
control signals (e.g. ``resume``) into a running ``Runner``.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from detonator.config import DetonatorConfig
from detonator.enrichment.pipeline import EnrichmentPipeline
from detonator.orchestrator.runner import Runner
from detonator.providers.vm.base import VMProvider
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore

logger = logging.getLogger(__name__)


class AppState:
    """Holds orchestrator dependencies and active run tracking."""

    def __init__(
        self,
        *,
        config: DetonatorConfig,
        vm_provider: VMProvider,
        database: Database,
        artifact_store: ArtifactStore,
        enrichment_pipeline: EnrichmentPipeline | None = None,
    ) -> None:
        self.config = config
        self.vm_provider = vm_provider
        self.database = database
        self.artifact_store = artifact_store
        self.enrichment_pipeline = enrichment_pipeline
        self._runners: dict[UUID, Runner] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}

    def register(self, runner: Runner, task: asyncio.Task) -> None:
        self._runners[runner.run_id] = runner
        self._tasks[runner.run_id] = task
        task.add_done_callback(lambda _t, rid=runner.run_id: self._cleanup(rid))

    def _cleanup(self, run_id: UUID) -> None:
        self._runners.pop(run_id, None)
        self._tasks.pop(run_id, None)

    def get_runner(self, run_id: UUID) -> Runner | None:
        return self._runners.get(run_id)

    def active_run_ids(self) -> list[UUID]:
        return list(self._runners.keys())

    async def shutdown(self) -> None:
        """Cancel any in-flight runs and close backing resources."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.database.close()
