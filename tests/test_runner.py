"""Tests for the orchestrator Runner state machine.

Uses stubbed VMProvider + fake AgentClient to drive a full run
without any real VM or HTTP traffic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from detonator.config import (
    AgentInstanceConfig,
    DetonatorConfig,
    StorageConfig,
    TimeoutsConfig,
)
from detonator.models import EgressType, NetworkInfo, RunConfig, RunState, VMInfo, VMState
from detonator.orchestrator.runner import Runner
from detonator.providers.vm.base import VMProvider
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore


class StubVMProvider(VMProvider):
    def __init__(self) -> None:
        self.revert_called_with: tuple[str, str] | None = None
        self.started = False
        self.stopped = False
        self.ip = "10.0.0.42"

    async def configure(self, config: dict) -> None:  # pragma: no cover
        pass

    async def list_vms(self) -> list[VMInfo]:  # pragma: no cover
        return []

    async def get_state(self, vm_id: str) -> VMState:
        return VMState.RUNNING

    async def revert(self, vm_id: str, snapshot_id: str) -> None:
        self.revert_called_with = (vm_id, snapshot_id)

    async def start(self, vm_id: str) -> None:
        self.started = True

    async def stop(self, vm_id: str, *, force: bool = False) -> None:
        self.stopped = True

    async def get_console_url(self, vm_id: str) -> str:  # pragma: no cover
        return ""

    async def get_network_info(self, vm_id: str) -> NetworkInfo:
        return NetworkInfo(ip_address=self.ip, mac_address="AA:BB:CC:DD:EE:FF", bridge="vmbr1")


class FakeAgentClient:
    """Drop-in replacement for ``AgentClient`` used by the runner."""

    instances: list[FakeAgentClient] = []

    def __init__(
        self,
        base_url: str,
        *,
        statuses: list[str] | None = None,
        artifacts: dict[str, bytes] | None = None,
        health_ok: bool = True,
    ) -> None:
        self.base_url = base_url
        self._statuses = statuses or ["running", "complete"]
        self._idx = 0
        self._artifacts = artifacts or {"har_full.har": b'{"log":{}}'}
        self._health_ok = health_ok
        self.detonate_calls: list[dict] = []
        self.resumed = False
        FakeAgentClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def wait_for_health(self, *, timeout_sec, poll_sec=2.0):
        if not self._health_ok:
            raise TimeoutError("never healthy")
        return {"status": "ok"}

    async def detonate(self, url, **kwargs):
        self.detonate_calls.append({"url": url, **kwargs})

    async def status(self):
        from detonator.orchestrator.agent_manager import AgentStatus
        idx = min(self._idx, len(self._statuses) - 1)
        self._idx += 1
        return AgentStatus(state=self._statuses[idx])

    async def wait_for_terminal(self, *, timeout_sec, poll_sec=2.0, pause_on_interactive=False):
        from detonator.orchestrator.agent_manager import AgentStatus
        # Walk through scripted states, honoring pause_on_interactive.
        while self._idx < len(self._statuses):
            state = self._statuses[self._idx]
            self._idx += 1
            if state in {"complete", "error"}:
                return AgentStatus(state=state)
            if state == "paused" and pause_on_interactive:
                return AgentStatus(state=state)
        return AgentStatus(state="complete")

    async def resume(self):
        self.resumed = True
        from detonator.orchestrator.agent_manager import AgentStatus
        return AgentStatus(state="running")

    async def list_artifacts(self):
        return list(self._artifacts.keys())

    async def download_all(self, dest_dir: Path):
        results = []
        for name, data in self._artifacts.items():
            dest = dest_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            results.append((name, dest, len(data)))
        return results


@pytest.fixture(autouse=True)
def _reset_fake_clients():
    FakeAgentClient.instances.clear()
    yield
    FakeAgentClient.instances.clear()


@pytest.fixture
async def setup(tmp_path):
    """Assembles a Runner with in-memory SQLite + tmpdir artifact store."""
    agent = AgentInstanceConfig(
        name="sandbox",
        vm_id="100",
        snapshot="clean",
        port=8000,
        health_timeout_sec=1,
        health_poll_sec=1,
    )
    config = DetonatorConfig(
        agents=[agent],
        storage=StorageConfig(data_dir=str(tmp_path / "data"), db_path=":memory:"),
        timeouts=TimeoutsConfig(
            provision_sec=5, preflight_sec=5, detonate_sec=5, collect_sec=5, enrich_sec=5
        ),
    )
    database = Database(":memory:")
    await database.connect()
    store = ArtifactStore(str(tmp_path / "data"))
    vm = StubVMProvider()

    run_config = RunConfig(url="https://example.com", egress=EgressType.DIRECT)

    def _make_runner(**overrides):
        return Runner(
            config=config,
            agent=overrides.get("agent", agent),
            vm_provider=vm,
            database=database,
            artifact_store=store,
            run_config=overrides.get("run_config", run_config),
        )

    yield {
        "config": config,
        "agent": agent,
        "database": database,
        "store": store,
        "vm": vm,
        "run_config": run_config,
        "make_runner": _make_runner,
    }
    await database.close()


async def test_runner_happy_path(setup):
    def fake_client_factory(base_url, **_):
        return FakeAgentClient(base_url, statuses=["running", "complete"])

    with patch("detonator.orchestrator.runner.AgentManager", side_effect=fake_client_factory):
        runner = setup["make_runner"]()
        record = await runner.execute()

    assert record.state == RunState.COMPLETE
    assert record.completed_at is not None
    assert record.error is None

    # VM was cycled
    assert setup["vm"].revert_called_with == ("100", "clean")
    assert setup["vm"].started is True
    assert setup["vm"].stopped is True

    # Artifact was downloaded + indexed
    artifacts = await setup["database"].get_artifacts(str(runner.run_id))
    names = {Path(a["path"]).name for a in artifacts}
    assert "har_full.har" in names
    assert "meta.json" in names  # runner always dumps its meta snapshot

    # State transitions include all the major phases
    seen = {t.to_state for t in record.transitions}
    assert RunState.PROVISIONING in seen
    assert RunState.DETONATING in seen
    assert RunState.COLLECTING in seen
    assert RunState.COMPLETE in seen


async def test_runner_records_error_on_agent_failure(setup):
    def fake_client_factory(base_url, **_):
        return FakeAgentClient(base_url, statuses=["error"])

    with patch("detonator.orchestrator.runner.AgentManager", side_effect=fake_client_factory):
        runner = setup["make_runner"]()
        record = await runner.execute()

    assert record.state == RunState.ERROR
    assert record.error is not None
    row = await setup["database"].get_run(str(runner.run_id))
    assert row["status"] == RunState.ERROR.value
    assert row["error"] is not None


async def test_runner_fails_without_vm_ip(setup):
    def fake_client_factory(base_url, **_):
        return FakeAgentClient(base_url)

    setup["vm"].ip = None

    with patch("detonator.orchestrator.runner.AgentManager", side_effect=fake_client_factory):
        runner = setup["make_runner"]()
        record = await runner.execute()

    assert record.state == RunState.ERROR
    assert "did not report an IP" in record.error


async def test_runner_requires_vm_id_and_snapshot(setup, tmp_path):
    agent = setup["agent"].model_copy(update={"vm_id": "", "snapshot": ""})
    runner = Runner(
        config=setup["config"],
        agent=agent,
        vm_provider=setup["vm"],
        database=setup["database"],
        artifact_store=setup["store"],
        run_config=setup["run_config"],
    )
    record = await runner.execute()
    assert record.state == RunState.ERROR
    assert "vm_id" in record.error


async def test_runner_interactive_waits_for_resume(setup):
    def fake_client_factory(base_url, **_):
        return FakeAgentClient(base_url, statuses=["running", "paused", "complete"])

    run_config = RunConfig(url="https://example.com", interactive=True)

    with patch("detonator.orchestrator.runner.AgentManager", side_effect=fake_client_factory):
        runner = setup["make_runner"](run_config=run_config)

        import asyncio as _asyncio
        task = _asyncio.create_task(runner.execute())

        # Wait until runner is in INTERACTIVE state, then signal resume.
        for _ in range(200):
            if runner.record.state == RunState.INTERACTIVE:
                break
            await _asyncio.sleep(0.01)
        else:
            task.cancel()
            raise AssertionError("Runner never entered INTERACTIVE state")

        runner.signal_resume()
        record = await task

    assert record.state == RunState.COMPLETE
    fake = FakeAgentClient.instances[0]
    assert fake.resumed is True
