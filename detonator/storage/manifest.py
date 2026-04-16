"""Manifest assembler.

Consolidates everything the system knows about a completed run into a single
``manifest.json`` document: run configuration, artifact inventory, enrichment
summary, chain/filter statistics, and technique matches.  This is the
primary entry point for an analyst inspecting a finished run without querying
the database.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1


def build_manifest(
    *,
    run_id: str,
    run_row: dict[str, Any],
    artifacts: list[dict[str, Any]],
    technique_matches: list[dict[str, Any]],
    artifact_dir: Path,
) -> dict[str, Any]:
    """Assemble and return the manifest dict for one completed (or failed) run.

    Args:
        run_id: The run UUID string.
        run_row: Row dict from ``Database.get_run``.
        artifacts: Rows from ``Database.get_artifacts``.
        technique_matches: Rows from ``Database.get_technique_matches_for_run``
            (already joined with the ``techniques`` table).
        artifact_dir: Path to the run's artifact directory on disk.  Used to
            read ``enrichment.json`` and ``filter_result.json``.

    Returns:
        A JSON-serialisable dict suitable for writing to ``manifest.json``.
    """
    # Decode stored config JSON.
    config: dict[str, Any] = {}
    if run_row.get("config_json"):
        try:
            config = json.loads(run_row["config_json"])
        except Exception:
            logger.warning("manifest run=%s: failed to parse config_json", run_id)

    # Enrichment summary from enrichment.json (written by the pipeline).
    enrichment_summary: dict[str, Any] | None = None
    enrich_path = artifact_dir / "enrichment.json"
    if enrich_path.exists():
        try:
            raw = json.loads(enrich_path.read_text("utf-8"))
            results = raw.get("results", [])
            obs_count = sum(len(r.get("observables", [])) for r in results)
            enrichment_summary = {
                "enriched_at": raw.get("enriched_at"),
                "enricher_count": len(results),
                "observable_count": obs_count,
                "modules": [r["enricher"] for r in results if "enricher" in r],
            }
        except Exception:
            logger.warning("manifest run=%s: failed to parse enrichment.json", run_id)

    # Chain / filter summary from filter_result.json (written by the filter stage).
    chain_summary: dict[str, Any] | None = None
    filter_path = artifact_dir / "filter_result.json"
    if filter_path.exists():
        try:
            fr = json.loads(filter_path.read_text("utf-8"))
            chain_summary = {
                "chain_requests": fr.get("chain_requests", 0),
                "noise_requests": fr.get("noise_requests", 0),
                "technique_hit_count": len(fr.get("technique_hits", [])),
            }
        except Exception:
            logger.warning("manifest run=%s: failed to parse filter_result.json", run_id)

    # Artifact inventory — relative path names only for portability.
    artifact_inventory: list[dict[str, Any]] = []
    for a in artifacts:
        # Compute relative name from absolute path so the manifest is portable.
        try:
            rel_name = str(Path(a["path"]).relative_to(artifact_dir))
        except ValueError:
            rel_name = Path(a["path"]).name
        artifact_inventory.append(
            {
                "name": rel_name,
                "type": a["type"],
                "size": a.get("size"),
                "sha256": a.get("content_hash"),
            }
        )

    # Technique matches — decode evidence_json inline.
    tech_rows: list[dict[str, Any]] = []
    for m in technique_matches:
        evidence = None
        if m.get("evidence_json"):
            try:
                evidence = json.loads(m["evidence_json"])
            except Exception:
                pass
        tech_rows.append(
            {
                "technique_id": m.get("technique_id"),
                "name": m.get("name"),
                "description": m.get("description"),
                "signature_type": m.get("signature_type"),
                "confidence": m.get("confidence"),
                "evidence": evidence,
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "seed_url": run_row.get("seed_url"),
        "status": run_row.get("status"),
        "egress_type": run_row.get("egress_type"),
        "created_at": run_row.get("created_at"),
        "completed_at": run_row.get("completed_at"),
        "error": run_row.get("error"),
        "config": config,
        "artifacts": artifact_inventory,
    }

    if enrichment_summary is not None:
        manifest["enrichment"] = enrichment_summary

    if chain_summary is not None:
        manifest["chain"] = chain_summary

    if tech_rows:
        manifest["technique_matches"] = tech_rows

    return manifest
