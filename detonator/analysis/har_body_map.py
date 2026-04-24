"""Map Playwright HAR body attachment filenames to their originating entries."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


BodyDirection = Literal["request", "response"]
BodySource = Literal["har_file", "capture_manifest"]


@dataclass(frozen=True)
class HarBodyRef:
    """A reverse-mapped HAR body attachment.

    Playwright's ``record_har_content="attach"`` mode writes each unique
    request/response body as a separate file under ``bodies/`` and records
    its path as either ``entries[i].response.content._file`` (downloaded
    response) or ``entries[i].request.postData._file`` (uploaded request
    body, typically for POST/PUT). This struct carries which direction the
    file came from so callers can classify it correctly — a response body
    is a ``site_resource``, a request body is a ``request_body`` (outgoing
    evidence like telemetry beacons, form submissions, fingerprint uploads).

    ``source`` identifies which capture pipeline produced this ref:
    - ``"har_file"`` — from Playwright's HAR writer via ``map_body_files``
    - ``"capture_manifest"`` — from the network sidecar via ``load_capture_manifest``
    """

    url: str
    direction: BodyDirection
    method: str
    mime_type: str | None = None
    source: BodySource = field(default="har_file")
    captured_at: str | None = None


def map_body_files(har_path: Path) -> dict[str, HarBodyRef]:
    """Return ``{body_filename_basename: HarBodyRef}`` from a Playwright HAR.

    Walks every entry and indexes both ``response.content._file`` and
    ``request.postData._file``. When a body file is referenced by both
    directions (unusual but possible on content-hash collisions), the
    response reference wins — the downloaded body is the stronger piece
    of evidence. Entries with no ``_file`` field (redirects, empty bodies)
    are silently skipped.
    """
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("har_body_map: could not read %s: %s", har_path, exc)
        return {}

    try:
        entries = data["log"]["entries"]
    except (KeyError, TypeError) as exc:
        logger.warning("har_body_map: unexpected HAR structure in %s: %s", har_path, exc)
        return {}

    mapping: dict[str, HarBodyRef] = {}

    for entry in entries:
        try:
            request = entry["request"]
            url = request["url"]
            method = (request.get("method") or "").upper()
        except (KeyError, TypeError):
            continue

        started_at: str | None = entry.get("startedDateTime") or None

        # Request body (outgoing). Record first so a response ref on the
        # same basename can overwrite it.
        post_data = request.get("postData") or {}
        req_file = post_data.get("_file") if isinstance(post_data, dict) else None
        if req_file:
            basename = Path(req_file).name
            if basename and basename not in mapping:
                mapping[basename] = HarBodyRef(
                    url=url,
                    direction="request",
                    method=method,
                    mime_type=(post_data.get("mimeType") or None),
                    source="har_file",
                    captured_at=started_at,
                )

        # Response body (downloaded). Wins over a prior request-body mapping.
        response = entry.get("response") or {}
        content = response.get("content") or {}
        resp_file = content.get("_file") if isinstance(content, dict) else None
        if resp_file:
            basename = Path(resp_file).name
            existing = mapping.get(basename)
            if basename and (existing is None or existing.direction != "response"):
                mapping[basename] = HarBodyRef(
                    url=url,
                    direction="response",
                    method=method,
                    mime_type=(content.get("mimeType") or None),
                    source="har_file",
                    captured_at=started_at,
                )

    return mapping


def load_capture_manifest(run_dir: Path) -> dict[str, HarBodyRef]:
    """Return ``{basename: HarBodyRef}`` from the agent's ``bodies/manifest.jsonl``.

    Covers responses Playwright's HAR writer silently drops — main-frame
    document navigations, some POST responses.  The agent writes one JSONL
    line per capture event; this function flattens them to one ref per
    basename (first source per basename wins).

    Falls back to the legacy ``bodies/extra.json`` format for older runs.
    Missing file is not an error.
    """
    jsonl_path = run_dir / "bodies" / "manifest.jsonl"
    if jsonl_path.exists():
        return _load_jsonl(jsonl_path)

    legacy_path = run_dir / "bodies" / "extra.json"
    if legacy_path.exists():
        return _load_legacy_json(legacy_path)

    return {}


def _load_jsonl(path: Path) -> dict[str, HarBodyRef]:
    mapping: dict[str, HarBodyRef] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("har_body_map: could not read %s: %s", path, exc)
        return {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Partial last line from a mid-run crash — skip it.
            continue
        if not isinstance(entry, dict):
            continue
        basename = entry.get("basename")
        if not isinstance(basename, str) or not basename:
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        direction = entry.get("direction") or "response"
        if direction not in ("request", "response"):
            continue
        # Only index entries that have a body file (outcome ok/truncated).
        outcome = entry.get("capture_outcome")
        if outcome not in ("ok", "truncated"):
            continue
        mapping.setdefault(
            basename,
            HarBodyRef(
                url=url,
                direction=direction,
                method=(entry.get("method") or "GET").upper(),
                mime_type=entry.get("mime_type"),
                source="capture_manifest",
                captured_at=entry.get("captured_at") or None,
            ),
        )
    return mapping


def _load_legacy_json(path: Path) -> dict[str, HarBodyRef]:
    """Parse the old ``bodies/extra.json`` format (pre-v2)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("har_body_map: could not read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}

    mapping: dict[str, HarBodyRef] = {}
    for basename, entry in data.items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        direction = entry.get("direction") or "response"
        if direction not in ("request", "response"):
            continue
        mapping[basename] = HarBodyRef(
            url=url,
            direction=direction,
            method=(entry.get("method") or "GET").upper(),
            mime_type=entry.get("mime_type"),
            source="capture_manifest",
            captured_at=entry.get("captured_at") or None,
        )
    return mapping


# Legacy alias — callers should migrate to load_capture_manifest.
load_extra_bodies = load_capture_manifest
