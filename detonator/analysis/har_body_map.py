"""Map Playwright HAR body attachment filenames to their originating entries."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


BodyDirection = Literal["request", "response"]


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
    """

    url: str
    direction: BodyDirection
    method: str
    mime_type: str | None = None


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
                )

    return mapping
