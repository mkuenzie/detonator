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


async def test_artifact_source_url_round_trip(db: Database):
    """insert_artifact persists source_url when supplied, None otherwise."""
    await db.insert_run("r-src", "https://src.com", "direct", {}, "2026-04-09T00:00:00")

    await db.insert_artifact("r-src", "meta", "/runs/r-src/abc.bin", 512, "hash1")
    await db.insert_artifact(
        "r-src", "site_resource", "/runs/r-src/def.bin", 256, "hash2",
        source_url="https://src.com/script.js",
    )

    by_hash = {a["content_hash"]: a for a in await db.get_artifacts("r-src")}
    assert by_hash["hash1"]["source_url"] is None
    assert by_hash["hash2"]["source_url"] == "https://src.com/script.js"


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


async def _seed_graph(db: Database) -> None:
    """Shared fixture data: campaign + 2 observables (linked) + technique."""
    ts = "2026-04-20T00:00:00"
    await db.upsert_observable("o1", "domain", "evil.com", ts)
    await db.upsert_observable("o2", "ip", "1.2.3.4", ts)
    await db.link_observables("o1", "o2", "resolves_to", ts, confidence=0.9)
    await db.upsert_technique("t1", "Cloudflare Workers Abuse", "", "infrastructure")
    await db.insert_campaign("c1", "Phish April", "spring wave", ts)
    await db.db.execute(
        "INSERT INTO campaign_observables (campaign_id, observable_id, role) VALUES (?, ?, ?)",
        ("c1", "o1", "indicator"),
    )
    await db.db.execute(
        "INSERT INTO campaign_techniques (campaign_id, technique_id) VALUES (?, ?)",
        ("c1", "t1"),
    )
    await db.insert_run("r1", "https://evil.com", "direct", {}, ts)
    await db.link_campaign_run("c1", "r1")
    await db.db.commit()


async def test_get_campaign_detail(db: Database):
    await _seed_graph(db)
    detail = await db.get_campaign_detail("c1")
    assert detail is not None
    assert detail["name"] == "Phish April"
    assert len(detail["runs"]) == 1
    assert len(detail["observables"]) == 1
    assert detail["observables"][0]["role"] == "indicator"
    assert len(detail["techniques"]) == 1


async def test_get_campaign_detail_missing(db: Database):
    assert await db.get_campaign_detail("does-not-exist") is None


async def test_search_graph_nodes(db: Database):
    await _seed_graph(db)
    rows = await db.search_graph_nodes("evil")
    labels = {(r["node_type"], r["label"]) for r in rows}
    assert ("observable", "evil.com") in labels

    rows = await db.search_graph_nodes("Cloudflare")
    assert any(r["node_type"] == "technique" for r in rows)

    rows = await db.search_graph_nodes("Phish")
    assert any(r["node_type"] == "campaign" for r in rows)

    # Case-insensitive.
    rows = await db.search_graph_nodes("EVIL")
    assert any(r["node_type"] == "observable" for r in rows)


async def test_get_node_neighborhood_observable(db: Database):
    await _seed_graph(db)
    hood = await db.get_node_neighborhood("observable", "o1")
    assert hood is not None
    assert hood["center"]["id"] == "o1"
    neighbor_ids = {n["id"] for n in hood["neighbors"]}
    assert "o2" in neighbor_ids          # linked observable
    assert "c1" in neighbor_ids          # campaign containing this observable
    # Edge types include both the link relationship and the campaign role.
    edge_types = {e["edge_type"] for e in hood["edges"]}
    assert "resolves_to" in edge_types
    assert "indicator" in edge_types


async def test_get_node_neighborhood_technique(db: Database):
    await _seed_graph(db)
    hood = await db.get_node_neighborhood("technique", "t1")
    assert hood is not None
    assert hood["center"]["label"] == "Cloudflare Workers Abuse"
    assert len(hood["neighbors"]) == 1
    assert hood["neighbors"][0]["node_type"] == "campaign"
    assert hood["edges"][0]["edge_type"] == "employs"


async def test_get_node_neighborhood_campaign(db: Database):
    await _seed_graph(db)
    hood = await db.get_node_neighborhood("campaign", "c1")
    assert hood is not None
    types = {n["node_type"] for n in hood["neighbors"]}
    assert types == {"observable", "technique"}  # runs are excluded per MVP
    assert len(hood["edges"]) == 2


async def test_get_node_neighborhood_missing(db: Database):
    assert await db.get_node_neighborhood("observable", "nope") is None
    assert await db.get_node_neighborhood("technique", "nope") is None
    assert await db.get_node_neighborhood("campaign", "nope") is None
    assert await db.get_node_neighborhood("run", "nope") is None


async def test_upsert_observable_metadata_round_trip(db: Database):
    """Metadata written via upsert_observable_metadata surfaces in get_observable_detail."""
    ts = "2026-04-22T00:00:00"
    await db.upsert_observable("om-1", "certificate", "example.com (fp:abc123def456)", ts)
    await db.upsert_observable_metadata("om-1", {
        "fingerprint_sha256": "abc123",
        "subject_cn": "example.com",
        "not_after": "2027-01-01T00:00:00",
    })

    detail = await db.get_observable_detail("om-1")
    assert detail is not None
    assert detail["metadata"]["fingerprint_sha256"] == "abc123"
    assert detail["metadata"]["subject_cn"] == "example.com"
    assert detail["metadata"]["not_after"] == "2027-01-01T00:00:00"


async def test_upsert_observable_metadata_merge_semantics(db: Database):
    """Later calls replace per-key but don't remove keys set by earlier calls."""
    ts = "2026-04-22T00:00:00"
    await db.upsert_observable("om-2", "domain", "test.com", ts)
    await db.upsert_observable_metadata("om-2", {"key_a": "first", "key_b": "stays"})
    await db.upsert_observable_metadata("om-2", {"key_a": "second"})

    detail = await db.get_observable_detail("om-2")
    meta = detail["metadata"]
    assert meta["key_a"] == "second"
    assert meta["key_b"] == "stays"


async def test_upsert_observable_metadata_non_string_coerced(db: Database):
    """Non-string values are coerced to str."""
    ts = "2026-04-22T00:00:00"
    await db.upsert_observable("om-3", "ip", "1.2.3.4", ts)
    await db.upsert_observable_metadata("om-3", {"count": 42, "flag": True})

    detail = await db.get_observable_detail("om-3")
    assert detail["metadata"]["count"] == "42"
    assert detail["metadata"]["flag"] == "True"
