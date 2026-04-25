#!/usr/bin/env python3
"""capture_diff.py — compare HAR body attachments vs the agent's capture manifest.

For each given run, fetches ``har_full.har`` and ``bodies/manifest.jsonl``
from the orchestrator API and reports which (url, direction) pairs are
captured by:

  - HAR only      (Playwright's record_har_content="attach")
  - Manifest only (agent NetworkCapture + CDPResponseTap + RouteDocumentInterceptor)
  - Both

Decision input for whether to disable HAR body attach:

  If HAR-only is empty/trivial across a varied batch of runs, the agent's
  capture path covers everything HAR attach does, and HAR attach can be
  switched off — eliminating the duplicate site_resource artifact rows.

URL-based comparison is intentional. We're answering "is this URL captured
at all on each side", not "is every byte identical" — the dedupe decision
only needs the former.

Usage:
  scripts/capture_diff.py                       # 10 most recent complete runs
  scripts/capture_diff.py --limit 25
  scripts/capture_diff.py --run-id <uuid> --run-id <uuid>
  scripts/capture_diff.py --base http://bon-clay:8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict


def _fetch(base: str, path: str) -> bytes:
    url = f"{base.rstrip('/')}{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _list_recent_runs(base: str, limit: int) -> list[str]:
    body = _fetch(base, f"/runs?status=complete&limit={limit}")
    runs = json.loads(body)
    return [r["id"] for r in runs]


def _har_attached(har_bytes: bytes) -> set[tuple[str, str]]:
    """Set of (url, direction) for HAR entries with a body file attached."""
    har = json.loads(har_bytes)
    out: set[tuple[str, str]] = set()
    for entry in har.get("log", {}).get("entries", []):
        url = (entry.get("request") or {}).get("url") or ""
        if not url:
            continue
        post = (entry.get("request") or {}).get("postData") or {}
        if isinstance(post, dict) and post.get("_file"):
            out.add((url, "request"))
        content = (entry.get("response") or {}).get("content") or {}
        if isinstance(content, dict) and content.get("_file"):
            out.add((url, "response"))
    return out


def _manifest(jsonl_bytes: bytes) -> set[tuple[str, str]]:
    """Set of (url, direction) for manifest entries that wrote a body."""
    out: set[tuple[str, str]] = set()
    for line in jsonl_bytes.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("capture_outcome") not in ("ok", "truncated"):
            continue
        url = entry.get("url")
        direction = entry.get("direction") or "response"
        if isinstance(url, str) and url:
            out.add((url, direction))
    return out


def _diff_run(base: str, run_id: str) -> dict | None:
    try:
        har_bytes = _fetch(base, f"/runs/{run_id}/artifacts/har_full.har")
    except urllib.error.HTTPError as e:
        print(f"{run_id}: har_full.har fetch failed: {e}", file=sys.stderr)
        return None
    try:
        man_bytes = _fetch(base, f"/runs/{run_id}/artifacts/bodies/manifest.jsonl")
    except urllib.error.HTTPError:
        man_bytes = b""

    har_set = _har_attached(har_bytes)
    man_set = _manifest(man_bytes)
    return {
        "run_id": run_id,
        "har_total": len(har_set),
        "manifest_total": len(man_set),
        "both": len(har_set & man_set),
        "har_only": sorted(har_set - man_set),
        "manifest_only": sorted(man_set - har_set),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--base",
        default=os.environ.get("DETONATOR_BASE", "http://bon-clay:8080"),
        help="Orchestrator base URL (default: $DETONATOR_BASE or http://bon-clay:8080)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent complete runs to scan when no --run-id is given",
    )
    ap.add_argument(
        "--run-id",
        action="append",
        help="Specific run id (repeatable; overrides --limit)",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=15,
        help="Sample URLs to print per asymmetric set",
    )
    args = ap.parse_args()

    if args.run_id:
        run_ids = args.run_id
    else:
        run_ids = _list_recent_runs(args.base, args.limit)

    if not run_ids:
        print("No runs to process", file=sys.stderr)
        return 1

    totals: dict[str, int] = defaultdict(int)
    all_har_only: list[tuple[str, str]] = []
    all_man_only: list[tuple[str, str]] = []
    runs_processed = 0

    print(f"{'run_id':<38} {'har':>5} {'man':>5} {'both':>5} {'har_only':>9} {'man_only':>9}")
    for rid in run_ids:
        d = _diff_run(args.base, rid)
        if d is None:
            continue
        runs_processed += 1
        totals["har_total"] += d["har_total"]
        totals["manifest_total"] += d["manifest_total"]
        totals["both"] += d["both"]
        totals["har_only"] += len(d["har_only"])
        totals["manifest_only"] += len(d["manifest_only"])
        print(
            f"{rid:<38} {d['har_total']:>5} {d['manifest_total']:>5} "
            f"{d['both']:>5} {len(d['har_only']):>9} {len(d['manifest_only']):>9}"
        )
        all_har_only.extend(d["har_only"])
        all_man_only.extend(d["manifest_only"])

    print()
    print(f"=== Aggregate over {runs_processed} run(s) ===")
    print(f"  har_total       : {totals['har_total']}")
    print(f"  manifest_total  : {totals['manifest_total']}")
    print(f"  both            : {totals['both']}")
    print(f"  har_only        : {totals['har_only']}   <-- danger set if HAR attach is disabled")
    print(f"  manifest_only   : {totals['manifest_only']}   <-- justification for agent capture")

    if all_har_only:
        print()
        print(f"--- HAR-only samples (would be lost if HAR attach is disabled) ---")
        seen: set[tuple[str, str]] = set()
        for url, direction in all_har_only:
            if (url, direction) in seen:
                continue
            seen.add((url, direction))
            print(f"  [{direction:<8}] {url}")
            if len(seen) >= args.samples:
                break

    if all_man_only:
        print()
        print(f"--- Manifest-only samples (URLs only the agent caught) ---")
        seen = set()
        for url, direction in all_man_only:
            if (url, direction) in seen:
                continue
            seen.add((url, direction))
            print(f"  [{direction:<8}] {url}")
            if len(seen) >= args.samples:
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
