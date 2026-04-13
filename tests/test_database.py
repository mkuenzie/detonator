"""Tests for the SQLite storage layer."""

import pytest
from detonator.storage.database import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def test_insert_and_get_run(db: Database):
    await db.insert_run("run-1", "https://example.com", "direct", {"url": "https://example.com"}, "2026-04-09T00:00:00")
    run = await db.get_run("run-1")
    assert run is not None
    assert run["seed_url"] == "https://example.com"
    assert run["status"] == "pending"


async def test_update_run_status(db: Database):
    await db.insert_run("run-2", "https://test.com", "vpn", {}, "2026-04-09T00:00:00")
    await db.update_run_status("run-2", "complete", completed_at="2026-04-09T00:01:00")
    run = await db.get_run("run-2")
    assert run["status"] == "complete"
    assert run["completed_at"] == "2026-04-09T00:01:00"


async def test_list_runs_with_filter(db: Database):
    await db.insert_run("r1", "https://a.com", "direct", {}, "2026-04-09T00:00:00")
    await db.insert_run("r2", "https://b.com", "direct", {}, "2026-04-09T00:01:00")
    await db.update_run_status("r1", "complete")

    all_runs = await db.list_runs()
    assert len(all_runs) == 2

    complete = await db.list_runs(status="complete")
    assert len(complete) == 1
    assert complete[0]["id"] == "r1"


async def test_delete_run(db: Database):
    await db.insert_run("r-del", "https://del.com", "direct", {}, "2026-04-09T00:00:00")
    assert await db.delete_run("r-del") is True
    assert await db.get_run("r-del") is None


async def test_artifacts(db: Database):
    await db.insert_run("r-art", "https://art.com", "direct", {}, "2026-04-09T00:00:00")
    await db.insert_artifact("r-art", "har_full", "/data/runs/r-art/har.json", 1024, "abc123")
    artifacts = await db.get_artifacts("r-art")
    assert len(artifacts) == 1
    assert artifacts[0]["type"] == "har_full"


async def test_observable_upsert_and_find(db: Database):
    await db.upsert_observable("obs-1", "domain", "example.com", "2026-04-09T00:00:00")
    await db.upsert_observable("obs-1", "domain", "example.com", "2026-04-10T00:00:00")

    results = await db.find_observables(obs_type="domain")
    assert len(results) == 1
    assert results[0]["last_seen"] == "2026-04-10T00:00:00"


async def test_observable_graph(db: Database):
    await db.upsert_observable("o1", "domain", "evil.com", "2026-04-09T00:00:00")
    await db.upsert_observable("o2", "ip", "1.2.3.4", "2026-04-09T00:00:00")
    await db.link_observables("o1", "o2", "resolves_to", "2026-04-09T00:00:00")

    graph = await db.get_observable_graph("o1")
    assert len(graph["outgoing_links"]) == 1
    assert graph["outgoing_links"][0]["relationship"] == "resolves_to"


async def test_campaign_lifecycle(db: Database):
    await db.insert_campaign("c1", "Test Campaign", "For testing", "2026-04-09T00:00:00")
    await db.insert_run("r-c", "https://c.com", "direct", {}, "2026-04-09T00:00:00")
    await db.link_campaign_run("c1", "r-c")

    campaign = await db.get_campaign("c1")
    assert campaign is not None
    assert campaign["name"] == "Test Campaign"


async def test_technique_match(db: Database):
    await db.upsert_technique("t1", "Google Storage Hosting", "Phishing via GCS", "infrastructure")
    await db.insert_run("r-t", "https://t.com", "direct", {}, "2026-04-09T00:00:00")
    await db.insert_technique_match("t1", "r-t", confidence=0.9, evidence={"url": "storage.googleapis.com/phish"})
