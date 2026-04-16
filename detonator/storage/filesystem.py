"""Filesystem-based artifact storage."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class ArtifactStore:
    """Manages the on-disk artifact directory tree.

    Layout::

        {base_dir}/
          runs/
            {run-id}/
              har_full.har
              har_chain.json
              screenshots/
              dom.html
              console.json
              enrichment/
              meta.json
              manifest.json
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)

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
        return d

    def store_file(self, run_id: str, name: str, source: Path) -> tuple[Path, int, str]:
        """Copy a file into the run directory and return (dest, size, sha256)."""
        dest_dir = self.ensure_run_dir(run_id)

        if "/" in name:
            (dest_dir / name).parent.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / name
        shutil.copy2(source, dest)
        size = dest.stat().st_size
        content_hash = self._sha256(dest)
        return dest, size, content_hash

    def store_bytes(self, run_id: str, name: str, data: bytes) -> tuple[Path, int, str]:
        """Write bytes into the run directory and return (path, size, sha256)."""
        dest_dir = self.ensure_run_dir(run_id)

        if "/" in name:
            (dest_dir / name).parent.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / name
        dest.write_bytes(data)
        content_hash = hashlib.sha256(data).hexdigest()
        return dest, len(data), content_hash

    def list_artifacts(self, run_id: str) -> list[str]:
        """Return relative paths of all files in a run directory."""
        d = self.run_dir(run_id)
        if not d.exists():
            return []
        return [str(f.relative_to(d)) for f in d.rglob("*") if f.is_file()]

    def get_artifact_path(self, run_id: str, name: str) -> Path | None:
        path = self.run_dir(run_id) / name
        if path.exists() and path.is_relative_to(self.run_dir(run_id)):
            return path
        return None

    def delete_run(self, run_id: str) -> bool:
        d = self.run_dir(run_id)
        if d.exists():
            shutil.rmtree(d)
            return True
        return False

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
