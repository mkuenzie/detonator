"""Map Playwright HAR body attachment filenames to their originating request URLs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def map_body_files_to_urls(har_path: Path) -> dict[str, str]:
    """Return ``{body_filename_basename: request_url}`` from a Playwright HAR.

    Playwright's ``record_har_content="attach"`` mode writes each unique
    response body as a separate file and records its path in
    ``entries[i].response.content._file``.  This function walks all entries
    and builds the reverse mapping so callers can stamp artifact rows with the
    originating URL.

    When multiple entries share a body file (deduplication by content hash),
    the first URL encountered is kept.  Entries with no ``_file`` field (e.g.
    redirects, empty bodies) are silently skipped.
    """
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("har_body_map: could not read %s: %s", har_path, exc)
        return {}

    entries = []
    try:
        entries = data["log"]["entries"]
    except (KeyError, TypeError) as exc:
        logger.warning("har_body_map: unexpected HAR structure in %s: %s", har_path, exc)
        return {}

    mapping: dict[str, str] = {}
    for entry in entries:
        try:
            file_ref: str | None = entry["response"]["content"].get("_file")
            request_url: str = entry["request"]["url"]
        except (KeyError, TypeError):
            continue

        if not file_ref:
            continue

        basename = Path(file_ref).name
        if basename and basename not in mapping:
            mapping[basename] = request_url

    return mapping
