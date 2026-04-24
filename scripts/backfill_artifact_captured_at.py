"""One-shot backfill: populate artifacts.captured_at for existing runs.

For each run, builds a source_url -> captured_at map from har_full.har
(startedDateTime) and bodies/manifest.jsonl (captured_at field), then
UPDATEs artifact rows that have a matching source_url and no captured_at yet.
HAR wins on conflict — mirrors the precedence used during collection.

Usage:
    python scripts/backfill_artifact_captured_at.py [--db PATH] [--artifact-dir PATH]

Defaults read from config.toml in the repo root (storage.db_path and
storage.artifact_dir) if not overridden on the command line.

Safe to re-run: skips rows that already have a captured_at value.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _index_har(har_path: Path) -> dict[str, str]:
    """Return {url: startedDateTime} from a Playwright HAR."""
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  warn: could not read {har_path}: {exc}", file=sys.stderr)
        return {}
    result: dict[str, str] = {}
    for entry in data.get("log", {}).get("entries", []):
        url = (entry.get("request") or {}).get("url")
        started = entry.get("startedDateTime")
        if url and started and url not in result:
            result[url] = started
    return result


def _index_manifest(run_dir: Path) -> dict[str, str]:
    """Return {url: captured_at} from bodies/manifest.jsonl (or extra.json)."""
    jsonl = run_dir / "bodies" / "manifest.jsonl"
    if jsonl.exists():
        result: dict[str, str] = {}
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url")
                ts = entry.get("captured_at")
                if url and ts and url not in result:
                    result[url] = ts
        except Exception as exc:
            print(f"  warn: could not read {jsonl}: {exc}", file=sys.stderr)
        return result

    legacy = run_dir / "bodies" / "extra.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return {}
        result = {}
        for entry in (data.values() if isinstance(data, dict) else []):
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            ts = entry.get("captured_at")
            if url and ts and url not in result:
                result[url] = ts
        return result

    return {}


def _load_config_defaults() -> tuple[str | None, str | None]:
    """Read db_path and artifact_dir from config.toml if present."""
    config_path = Path(__file__).parent.parent / "config.toml"
    if not config_path.exists():
        return None, None
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None, None
    try:
        cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        storage = cfg.get("storage") or {}
        return storage.get("db_path"), storage.get("artifact_dir")
    except Exception:
        return None, None


def main() -> None:
    default_db, default_artifacts = _load_config_defaults()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_db, help="Path to detonator.db")
    parser.add_argument("--artifact-dir", default=default_artifacts, help="Artifact storage root")
    args = parser.parse_args()

    if not args.db:
        print("error: --db is required (or set storage.db_path in config.toml)", file=sys.stderr)
        sys.exit(1)
    if not args.artifact_dir:
        print("error: --artifact-dir is required (or set storage.artifact_dir in config.toml)", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db)
    artifact_root = Path(args.artifact_dir)

    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # Fetch all artifact rows for network types that lack captured_at.
    rows = con.execute(
        """SELECT a.id, a.run_id, a.source_url
           FROM artifacts a
           WHERE a.type IN ('site_resource', 'request_body')
             AND a.source_url IS NOT NULL
             AND (a.captured_at IS NULL OR a.captured_at = '')"""
    ).fetchall()

    if not rows:
        print("No artifact rows need backfilling.")
        con.close()
        return

    print(f"Found {len(rows)} artifact rows to backfill.")

    # Build per-run timestamp maps (lazily, one run at a time).
    run_maps: dict[str, dict[str, str]] = {}
    updated = 0
    skipped = 0

    for row in rows:
        run_id = row["run_id"]
        if run_id not in run_maps:
            run_dir = artifact_root / run_id
            har_map = _index_har(run_dir / "har_full.har") if (run_dir / "har_full.har").exists() else {}
            manifest_map = _index_manifest(run_dir)
            # HAR wins on conflict.
            merged: dict[str, str] = {**manifest_map, **har_map}
            run_maps[run_id] = merged

        ts = run_maps[run_id].get(row["source_url"])
        if ts:
            con.execute(
                "UPDATE artifacts SET captured_at = ? WHERE id = ?",
                (ts, row["id"]),
            )
            updated += 1
        else:
            skipped += 1

    con.commit()
    con.close()

    print(f"Updated: {updated}  |  No timestamp found (skipped): {skipped}")


if __name__ == "__main__":
    main()
