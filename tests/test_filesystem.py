"""Tests for filesystem artifact storage."""

from pathlib import Path

from detonator.storage.filesystem import ArtifactStore


def test_ensure_run_dir(tmp_path):
    store = ArtifactStore(tmp_path)
    d = store.ensure_run_dir("run-1")
    assert d.exists()
    assert (d / "screenshots").exists()
    assert (d / "enrichment").exists()


def test_store_bytes_and_retrieve(tmp_path):
    store = ArtifactStore(tmp_path)
    path, size, sha = store.store_bytes("run-1", "test.txt", b"hello world")
    assert path.exists()
    assert size == 11
    assert len(sha) == 64  # sha256 hex

    found = store.get_artifact_path("run-1", "test.txt")
    assert found == path


def test_store_file(tmp_path):
    store = ArtifactStore(tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"key": "value"}')

    path, size, sha = store.store_file("run-1", "data.json", source)
    assert path.exists()
    assert path.read_text() == '{"key": "value"}'


def test_list_artifacts(tmp_path):
    store = ArtifactStore(tmp_path)
    store.store_bytes("run-1", "a.txt", b"a")
    store.store_bytes("run-1", "b.txt", b"b")
    store.store_bytes("run-1", "screenshots/s1.png", b"png")

    artifacts = store.list_artifacts("run-1")
    assert len(artifacts) == 3
    assert "a.txt" in artifacts


def test_delete_run(tmp_path):
    store = ArtifactStore(tmp_path)
    store.store_bytes("run-del", "f.txt", b"x")
    assert store.delete_run("run-del") is True
    assert not store.run_dir("run-del").exists()
    assert store.delete_run("run-del") is False


def test_get_artifact_path_missing(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.get_artifact_path("no-run", "no-file") is None
