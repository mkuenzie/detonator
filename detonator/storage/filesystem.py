"""Filesystem-based artifact storage with content-addressable blob library.

Layout::

    {base_dir}/
      blobs/
        {xx}/
          {62 chars}           # real file, immutable, sha256-named
      runs/
        {run-id}/
          meta.json            -> ../../blobs/{xx}/{...}
          har_full.har         -> ...
          screenshots/
            0001.png           -> ../../../blobs/{xx}/{...}
          enrichment/
            whois.json         -> ...

Symlink targets are relative so the tree is portable across moves of base_dir.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


class ArtifactStore:
    """Manages the on-disk artifact directory tree backed by a CAS blob store."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)
        self._blobs = self._base / "blobs"

    @property
    def base_dir(self) -> Path:
        return self._base

    def run_dir(self, run_id: str) -> Path:
        return self._base / "runs" / run_id

    def ensure_run_dir(self, run_id: str) -> Path:
        d = self.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "screenshots").mkdir(exist_ok=True)
        (d / "enrichment").mkdir(exist_ok=True)
        self._blobs.mkdir(parents=True, exist_ok=True)
        return d

    # ── CAS helpers ──────────────────────────────────────────────

    def _blob_path(self, sha: str) -> Path:
        return self._blobs / sha[:2] / sha[2:]

    def _ingest_bytes(self, data: bytes) -> tuple[Path, str]:
        """Write bytes into the blob store (dedup by sha256). Returns (blob_path, sha)."""
        sha = hashlib.sha256(data).hexdigest()
        dest = self._blob_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.parent / f".tmp-{os.getpid()}-{os.urandom(4).hex()}"
            tmp.write_bytes(data)
            try:
                os.replace(tmp, dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        return dest, sha

    def _ingest_file(self, source: Path) -> tuple[Path, str]:
        """Move *source* into the blob store (dedup by sha256). Returns (blob_path, sha).

        Consumes *source* — after return the original path no longer exists.
        """
        sha = self._sha256_stream(source)
        dest = self._blob_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            try:
                os.replace(source, dest)
            except OSError:
                # Cross-device link: fall back to copy then unlink.
                shutil.copy2(source, dest)
                source.unlink()
        else:
            # Blob already present — identical content, just drop the source.
            source.unlink()
        return dest, sha

    def _link(self, run_id: str, name: str, blob_path: Path) -> Path:
        """Create (or confirm) a relative symlink at run_dir/name → blob_path."""
        link_path = self.run_dir(run_id) / name
        link_path.parent.mkdir(parents=True, exist_ok=True)
        rel_target = Path(os.path.relpath(blob_path, link_path.parent))
        if link_path.is_symlink():
            if os.readlink(link_path) == str(rel_target):
                return link_path  # already correct — no-op
            link_path.unlink()
        elif link_path.exists():
            # Regular file at that path (shouldn't happen in normal flow, but be safe).
            link_path.unlink()
        link_path.symlink_to(rel_target)
        return link_path

    @staticmethod
    def _sha256_stream(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Public API ───────────────────────────────────────────────

    def store_bytes(self, run_id: str, name: str, data: bytes) -> tuple[Path, int, str]:
        """Write bytes into the CAS and symlink from the run dir. Returns (symlink_path, size, sha256)."""
        self._blobs.mkdir(parents=True, exist_ok=True)
        blob_path, sha = self._ingest_bytes(data)
        link_path = self._link(run_id, name, blob_path)
        return link_path, len(data), sha

    def store_file(self, run_id: str, name: str, source: Path) -> tuple[Path, int, str]:
        """Move *source* into the CAS and symlink from the run dir. Returns (symlink_path, size, sha256).

        Note: this **consumes** source — the file is moved into the blob store.
        """
        self._blobs.mkdir(parents=True, exist_ok=True)
        size = source.stat().st_size
        blob_path, sha = self._ingest_file(source)
        link_path = self._link(run_id, name, blob_path)
        return link_path, size, sha

    def adopt(self, run_id: str, name: str, existing_path: Path) -> tuple[Path, int, str]:
        """Move a file already at *existing_path* (typically run_dir/name) into the CAS.

        Replaces the original file with a symlink to the blob. Used by the
        collection pipeline after ``agent.download_all`` drops files into the run dir.
        Returns (symlink_path, size, sha256).
        """
        self._blobs.mkdir(parents=True, exist_ok=True)
        size = existing_path.stat().st_size
        blob_path, sha = self._ingest_file(existing_path)
        link_path = self._link(run_id, name, blob_path)
        return link_path, size, sha

    def get_artifact_path(self, run_id: str, name: str) -> Path | None:
        path = self.run_dir(run_id) / name
        # path.exists() follows symlinks; is_relative_to checks the symlink path itself.
        if path.exists() and path.is_relative_to(self.run_dir(run_id)):
            return path
        return None

    def delete_run(self, run_id: str) -> bool:
        """Remove the run's symlink tree. Blobs are NOT removed here — use delete_blobs()."""
        d = self.run_dir(run_id)
        if d.exists():
            shutil.rmtree(d)
            return True
        return False

    def delete_blobs(self, hashes: Iterable[str]) -> int:
        """Unlink blobs for the given sha256 hashes. Prunes empty prefix dirs.

        Safe to call with already-missing hashes. Returns count of blobs removed.
        """
        count = 0
        for sha in hashes:
            blob = self._blob_path(sha)
            if blob.exists():
                blob.unlink()
                count += 1
            prefix_dir = blob.parent
            if prefix_dir.exists():
                try:
                    prefix_dir.rmdir()  # only succeeds if now empty
                except OSError:
                    pass
        return count
