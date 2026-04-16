"""Tests for filesystem artifact storage (CAS-backed)."""

from pathlib import Path

from detonator.storage.filesystem import ArtifactStore


def test_ensure_run_dir(tmp_path):
    store = ArtifactStore(tmp_path)
    d = store.ensure_run_dir("run-1")
    assert d.exists()
    assert (d / "screenshots").exists()
    assert (d / "enrichment").exists()
    assert (tmp_path / "blobs").exists()


def test_store_bytes_and_retrieve(tmp_path):
    store = ArtifactStore(tmp_path)
    path, size, sha = store.store_bytes("run-1", "test.txt", b"hello world")

    assert path.is_symlink(), "stored artifact must be a symlink"
    assert path.resolve().is_relative_to(tmp_path / "blobs"), "symlink must resolve into blobs/"
    assert path.read_bytes() == b"hello world"
    assert size == 11
    assert len(sha) == 64  # sha256 hex

    found = store.get_artifact_path("run-1", "test.txt")
    assert found == path


def test_store_file(tmp_path):
    store = ArtifactStore(tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"key": "value"}')

    path, size, sha = store.store_file("run-1", "data.json", source)

    assert path.is_symlink(), "stored artifact must be a symlink"
    assert path.resolve().is_relative_to(tmp_path / "blobs")
    assert path.read_text() == '{"key": "value"}'
    # source should be consumed (moved into CAS)
    assert not source.exists(), "store_file should consume (move) the source file"


def test_delete_run(tmp_path):
    store = ArtifactStore(tmp_path)
    store.store_bytes("run-del", "f.txt", b"x")

    blob_dir = tmp_path / "blobs"
    blobs_before = list(blob_dir.rglob("*"))

    assert store.delete_run("run-del") is True
    assert not store.run_dir("run-del").exists()
    assert store.delete_run("run-del") is False

    # Blobs must survive run deletion — GC is separate.
    assert list(blob_dir.rglob("*")) == blobs_before


def test_get_artifact_path_missing(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.get_artifact_path("no-run", "no-file") is None


def test_dedup_across_runs(tmp_path):
    """Two runs storing identical bytes should share a single blob on disk."""
    store = ArtifactStore(tmp_path)
    data = b"shared payload"

    path_a, _, sha_a = store.store_bytes("run-a", "payload.bin", data)
    path_b, _, sha_b = store.store_bytes("run-b", "payload.bin", data)

    assert sha_a == sha_b, "hashes must match for identical content"

    blob_files = [f for f in (tmp_path / "blobs").rglob("*") if f.is_file()]
    assert len(blob_files) == 1, "only one physical blob should exist"

    assert path_a.resolve() == path_b.resolve(), "both symlinks must point to the same blob"


def test_delete_blobs_orphans_only(tmp_path):
    """delete_blobs should only remove the targeted blobs; prunes empty prefix dirs."""
    store = ArtifactStore(tmp_path)
    data = b"shared content"

    _, _, sha = store.store_bytes("run-a", "f.bin", data)
    store.store_bytes("run-b", "f.bin", data)  # same blob, different run

    blob = (tmp_path / "blobs" / sha[:2] / sha[2:])
    assert blob.exists()

    # If both runs still reference the blob the caller would pass an empty list,
    # so calling delete_blobs with this hash now (while run-b still has a ref in
    # the filesystem) validates the mechanics of the method itself.
    count = store.delete_blobs([sha])
    assert count == 1
    assert not blob.exists()
    assert not blob.parent.exists(), "empty prefix dir should be pruned"

    # Calling again with already-gone hash is a no-op.
    count2 = store.delete_blobs([sha])
    assert count2 == 0


def test_adopt_consumes_source(tmp_path):
    """adopt() moves an existing file into the CAS and replaces it with a symlink."""
    store = ArtifactStore(tmp_path)
    run_dir = store.ensure_run_dir("run-adopt")

    # Simulate agent.download_all dropping a file directly in the run dir.
    raw_file = run_dir / "har_full.har"
    raw_file.write_bytes(b"HAR content here")

    symlink_path, size, sha = store.adopt("run-adopt", "har_full.har", raw_file)

    assert symlink_path == raw_file, "returned path must be the original location"
    assert symlink_path.is_symlink(), "original path must now be a symlink"
    assert symlink_path.resolve().is_relative_to(tmp_path / "blobs")
    assert symlink_path.read_bytes() == b"HAR content here"
    assert size == len(b"HAR content here")
    assert len(sha) == 64
