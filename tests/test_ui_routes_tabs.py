"""Smoke tests for the tab partials, transaction detail, and body preview routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from detonator.config import AgentInstanceConfig, DetonatorConfig, StorageConfig
from detonator.orchestrator.api import create_app
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore
from tests.test_runner import StubVMProvider


SAMPLE_HAR = {
    "log": {
        "version": "1.2",
        "entries": [
            {
                "startedDateTime": "2024-01-01T00:00:00.000Z",
                "time": 42.5,
                "request": {
                    "method": "GET",
                    "url": "https://example.com/",
                    "headers": [{"name": "Accept", "value": "*/*"}],
                    "bodySize": -1,
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "Content-Type", "value": "text/html"}],
                    "content": {
                        "size": 100,
                        "mimeType": "text/html",
                        "_file": "bodies/aabbcc.html",
                    },
                },
                "_resourceType": "document",
                "_initiator": {"type": "other"},
            }
        ],
    }
}


@pytest.fixture
def client_with_run(tmp_path: Path):
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

    app = create_app(config, vm_provider=vm, database=database, artifact_store=store)

    with TestClient(app) as client:
        resp = client.post("/runs", json={"url": "https://example.com", "agent": "sandbox"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        run_dir = store.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "har_full.har").write_text(json.dumps(SAMPLE_HAR), encoding="utf-8")
        bodies_dir = run_dir / "bodies"
        bodies_dir.mkdir(exist_ok=True)
        body_bytes = b"<html><body>hello</body></html>"
        (bodies_dir / "aabbcc.html").write_bytes(body_bytes)

        yield client, run_id


def test_tab_overview(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/runs/{run_id}?tab=overview")
    assert resp.status_code == 200
    assert "overview" in resp.text


def test_tab_network(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/runs/{run_id}?tab=network")
    assert resp.status_code == 200
    assert "example.com" in resp.text


def test_tab_resources(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/runs/{run_id}?tab=resources")
    assert resp.status_code == 200


def test_tab_timeline(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/runs/{run_id}?tab=timeline")
    assert resp.status_code == 200


def test_tab_invalid_falls_back(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/runs/{run_id}?tab=doesnotexist")
    assert resp.status_code == 200
    assert "tab-strip" in resp.text


def test_partial_tab_network(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/tab/network")
    assert resp.status_code == 200
    assert "example.com" in resp.text


def test_partial_tab_overview(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/tab/overview")
    assert resp.status_code == 200


def test_partial_tab_unknown(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/tab/nonexistent")
    assert resp.status_code == 404


def test_partial_transaction_detail(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/transactions/0")
    assert resp.status_code == 200
    assert "https://example.com/" in resp.text


def test_partial_transaction_out_of_range(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/transactions/999")
    assert resp.status_code == 404


def test_body_preview_endpoint(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/bodies/aabbcc.html/preview")
    assert resp.status_code == 200
    assert "hello" in resp.text


def test_body_preview_path_traversal_rejected(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/bodies/../secret/preview")
    assert resp.status_code in (400, 404, 422)


def test_body_preview_unknown_basename(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/ui/_partials/runs/{run_id}/bodies/nothere.html/preview")
    assert resp.status_code == 404


def test_json_transactions_endpoint(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/runs/{run_id}/transactions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["url"] == "https://example.com/"
    assert data[0]["method"] == "GET"
    assert data[0]["status"] == 200
    assert data[0]["is_text"] is True


def test_json_transaction_detail_endpoint(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/runs/{run_id}/transactions/0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://example.com/"
    assert "request_headers" in data
    assert "response_headers" in data
    assert "initiator_chain" in data


def test_json_transaction_detail_not_found(client_with_run):
    client, run_id = client_with_run
    resp = client.get(f"/runs/{run_id}/transactions/999")
    assert resp.status_code == 404
