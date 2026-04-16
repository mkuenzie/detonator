"""Tests for the orchestrator FastAPI app.

Uses TestClient with stubbed VM provider so we never touch real infra.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from detonator.config import AgentInstanceConfig, DetonatorConfig, StorageConfig
from detonator.orchestrator.api import create_app
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore
from tests.test_runner import StubVMProvider


@pytest.fixture
def app_client(tmp_path: Path):
    config = DetonatorConfig(
        agents=[
            AgentInstanceConfig(
                name="sandbox",
                vm_id="100",
                snapshot="clean",
                port=8000,
                health_timeout_sec=1,
                health_poll_sec=1,
            )
        ],
        storage=StorageConfig(
            data_dir=str(tmp_path / "data"),
            db_path=str(tmp_path / "detonator.db"),
        ),
    )
    database = Database(str(tmp_path / "detonator.db"))
    store = ArtifactStore(str(tmp_path / "data"))
    vm = StubVMProvider()

    app = create_app(
        config, vm_provider=vm, database=database, artifact_store=store
    )
    with TestClient(app) as client:
        yield client, database, store


def test_health_endpoint(app_client):
    client, _, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["active_runs"] == 0


def test_config_egress_empty(app_client):
    client, _, _ = app_client
    resp = client.get("/config/egress")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_list_runs_empty(app_client):
    client, _, _ = app_client
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_run_not_found(app_client):
    client, _, _ = app_client
    resp = client.get("/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_delete_run_not_found(app_client):
    client, _, _ = app_client
    resp = client.delete("/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_resume_run_not_active(app_client):
    client, _, _ = app_client
    resp = client.post("/runs/00000000-0000-0000-0000-000000000000/resume")
    assert resp.status_code == 404


def test_campaign_crud_round_trip(app_client):
    client, _, _ = app_client

    # Create
    resp = client.post(
        "/campaigns", json={"name": "Test Campaign", "description": "initial"}
    )
    assert resp.status_code == 200
    campaign_id = resp.json()["id"]

    # Get
    resp = client.get(f"/campaigns/{campaign_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Test Campaign"
    assert body["runs"] == []
    assert body["observables"] == []
    assert body["techniques"] == []

    # Update
    resp = client.put(
        f"/campaigns/{campaign_id}",
        json={"description": "updated", "confidence": 0.8},
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated"
    assert resp.json()["confidence"] == 0.8

    # List
    resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_observables_empty(app_client):
    client, _, _ = app_client
    resp = client.get("/observables")
    assert resp.status_code == 200
    assert resp.json() == []


def test_techniques_empty(app_client):
    client, _, _ = app_client
    resp = client.get("/techniques")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_run_persists_and_schedules(app_client, monkeypatch):
    client, database, store = app_client

    # Stub Runner.execute to a no-op so no real detonation is attempted.
    # The runner's real state machine has its own dedicated tests.
    from detonator.orchestrator import runner as runner_module

    calls: list[str] = []

    async def fake_execute(self):
        calls.append(str(self.run_id))
        await self.database.insert_run(
            run_id=str(self.run_id),
            seed_url=self.record.config.url,
            egress_type=self.record.config.egress.value,
            config=self.record.config.model_dump(),
            created_at=self.record.created_at.isoformat(),
        )
        return self.record

    monkeypatch.setattr(runner_module.Runner, "execute", fake_execute)

    resp = client.post(
        "/runs",
        json={"url": "https://example.com", "egress": "direct"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "pending"
    assert "run_id" in body

    # Poll the DB (not the API) for the run row — the background task must
    # land at some point; if it doesn't within a second the wiring is broken.
    import time
    run_id = body["run_id"]
    for _ in range(100):
        r = client.get(f"/runs/{run_id}")
        if r.status_code == 200:
            assert r.json()["seed_url"] == "https://example.com"
            return
        time.sleep(0.01)
    raise AssertionError("Run never persisted to database")
