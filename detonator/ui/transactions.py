"""Transaction view-model for the Network tab.

Merges HAR entries with the agent's sidecar capture manifest into a flat list
of :class:`Transaction` rows.  All I/O is synchronous file reads — this module
is a pure composition helper, not a service.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from detonator.analysis.har_body_map import load_capture_manifest, map_body_files
from detonator.analysis.navigation import _extract_initiator_url, build_initiator_graph

logger = logging.getLogger(__name__)

_TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/ld+json",
    "application/manifest+json",
    "application/x-www-form-urlencoded",
)

_IMAGE_MIME_PREFIXES = ("image/",)

_RESOURCE_TYPE_MAP: list[tuple[tuple[str, ...], str]] = [
    # MIME prefix → HAR resource-type token (matches filter chip data-filter values)
    (("text/html", "application/xhtml+xml"), "document"),
    (("text/css",), "stylesheet"),
    (
        (
            "text/javascript",
            "application/javascript",
            "application/x-javascript",
            "text/ecmascript",
            "application/ecmascript",
        ),
        "script",
    ),
    (("image/",), "image"),
    (
        (
            "font/",
            "application/font-",
            "application/x-font-",
            "application/vnd.ms-fontobject",
        ),
        "font",
    ),
    (("audio/", "video/"), "media"),
    # JSON / plain-text responses are almost always data fetches
    (("application/json", "application/ld+json", "application/manifest+json"), "xhr"),
]

# File extension fallback for generic MIME types like application/octet-stream.
_EXT_RESOURCE_TYPE: dict[str, str] = {
    ".js": "script",
    ".mjs": "script",
    ".cjs": "script",
    ".css": "stylesheet",
    ".html": "document",
    ".htm": "document",
    ".xhtml": "document",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".svg": "image",
    ".webp": "image",
    ".avif": "image",
    ".ico": "image",
    ".woff": "font",
    ".woff2": "font",
    ".ttf": "font",
    ".otf": "font",
    ".eot": "font",
    ".mp4": "media",
    ".webm": "media",
    ".mp3": "media",
    ".ogg": "media",
    ".json": "xhr",
    ".jsonld": "xhr",
}

# MIME types so generic they carry no signal — fall through to extension lookup.
_OPAQUE_MIMES = frozenset({"application/octet-stream", "binary/octet-stream", "application/binary"})


def _infer_resource_type(mime: str | None, url: str = "") -> str:
    """Derive a HAR resource-type token from MIME type, with URL extension fallback."""
    base = mime.split(";")[0].strip().lower() if mime else ""

    if base and base not in _OPAQUE_MIMES:
        for prefixes, rtype in _RESOURCE_TYPE_MAP:
            if any(base == p or base.startswith(p) for p in prefixes):
                return rtype

    # MIME was absent or opaque — try the URL path extension.
    if url:
        try:
            path = urlparse(url).path
            ext = Path(path).suffix.lower()
            if ext in _EXT_RESOURCE_TYPE:
                return _EXT_RESOURCE_TYPE[ext]
        except Exception:
            pass

    return "other"


def _is_text_mime(mime: str | None) -> bool:
    if not mime:
        return False
    base = mime.split(";")[0].strip().lower()
    return any(base.startswith(p) for p in _TEXT_MIME_PREFIXES)


def _is_image_mime(mime: str | None) -> bool:
    if not mime:
        return False
    base = mime.split(";")[0].strip().lower()
    return any(base.startswith(p) for p in _IMAGE_MIME_PREFIXES)


def _strip_charset(mime: str | None) -> str | None:
    if not mime:
        return None
    return mime.split(";")[0].strip() or None


@dataclass
class Transaction:
    """One HTTP transaction row for the Network tab table."""

    index: int
    started_at: str | None
    time_ms: float | None
    method: str
    url: str
    status: int | None
    mime_type: str | None
    resource_type: str
    size_response: int | None
    size_request: int | None
    server_ip: str | None
    initiator_type: str
    initiator_url: str | None
    body_basename: str | None
    capture_outcome: str | None
    is_text: bool
    is_image: bool

    @property
    def host(self) -> str:
        try:
            return urlparse(self.url).netloc or self.url
        except Exception:
            return self.url

    @property
    def path(self) -> str:
        try:
            p = urlparse(self.url)
            full = p.path
            if p.query:
                full += "?" + p.query
            return full or "/"
        except Exception:
            return self.url

    @property
    def status_class(self) -> str:
        if self.status is None:
            return "status-unknown"
        if self.status < 300:
            return "status-2xx"
        if self.status < 400:
            return "status-3xx"
        if self.status < 500:
            return "status-4xx"
        return "status-5xx"


@dataclass
class TransactionDetail(Transaction):
    """Extended transaction row with full headers and initiator chain."""

    request_headers: list[tuple[str, str]] = field(default_factory=list)
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    frame_url: str | None = None
    initiator_chain: list[str] = field(default_factory=list)


@dataclass
class BodyPreview:
    """Result of reading and decoding a body file for inline preview."""

    mime_type: str | None
    is_text: bool
    is_image: bool
    text: str | None
    truncated: bool
    size_bytes: int
    basename: str


def _load_raw_har(run_dir: Path) -> dict:
    har_path = run_dir / "har_full.har"
    if not har_path.exists():
        return {}
    try:
        return json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("transactions: could not read HAR at %s: %s", har_path, exc)
        return {}


def load_transactions(run_dir: Path) -> list[Transaction]:
    """Merge HAR entries + sidecar manifest into an ordered list of Transaction rows.

    HAR is the primary source.  The sidecar manifest fills ``body_basename`` and
    ``capture_outcome`` for entries where Playwright's HAR attach did not record a
    ``_file`` ref.  Merge key: (url, method) with HAR winning on overlap.

    Duplicate HAR entries for the same (url, method) — where some have no captured
    body (size_response None) and others do — are collapsed into a single record.
    This is the common case when the sidecar captured a main-frame document that
    Playwright's HAR writer missed; both paths record the request but only one
    captures the body.
    """
    raw = _load_raw_har(run_dir)
    entries = raw.get("log", {}).get("entries", []) if raw else []

    har_refs = map_body_files(run_dir / "har_full.har") if (run_dir / "har_full.har").exists() else {}
    sidecar_refs = load_capture_manifest(run_dir)

    all_refs = {**sidecar_refs, **har_refs}

    transactions: list[Transaction] = []
    for idx, entry in enumerate(entries):
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        content = resp.get("content") or {}
        initiator = entry.get("_initiator") or {}

        url = req.get("url", "").strip()
        if not url:
            continue

        method = (req.get("method") or "GET").upper()
        status = resp.get("status")
        mime_raw = content.get("mimeType") or None
        mime = _strip_charset(mime_raw)
        resource_type = entry.get("_resourceType") or _infer_resource_type(mime_raw, url)
        server_ip = (entry.get("serverIPAddress") or "").strip("[] ") or None
        started_at = entry.get("startedDateTime") or None
        time_ms_raw = entry.get("time")
        time_ms = float(time_ms_raw) if time_ms_raw is not None else None
        size_resp = content.get("size")
        if size_resp is not None and size_resp < 0:
            size_resp = None
        size_req = req.get("bodySize")
        if size_req is not None and size_req < 0:
            size_req = None

        itype = initiator.get("type", "other") if initiator else "other"
        iurl = _extract_initiator_url(initiator)

        resp_file = content.get("_file")
        body_basename: str | None = None
        if resp_file:
            body_basename = Path(resp_file).name
        elif url in {ref.url for ref in all_refs.values() if ref.direction == "response"}:
            for bn, ref in all_refs.items():
                if ref.url == url and ref.direction == "response":
                    body_basename = bn
                    break

        capture_outcome: str | None = None
        sidecar = sidecar_refs.get(body_basename) if body_basename else None
        if sidecar:
            capture_outcome = None

        transactions.append(
            Transaction(
                index=idx,
                started_at=started_at,
                time_ms=time_ms,
                method=method,
                url=url,
                status=status,
                mime_type=mime,
                resource_type=resource_type,
                size_response=size_resp,
                size_request=size_req,
                server_ip=server_ip,
                initiator_type=itype,
                initiator_url=iurl,
                body_basename=body_basename,
                capture_outcome=capture_outcome,
                is_text=_is_text_mime(mime),
                is_image=_is_image_mime(mime),
            )
        )

    return _dedup_transactions(transactions)


def _dedup_transactions(txns: list[Transaction]) -> list[Transaction]:
    """Collapse HAR entries that represent the same request captured by different paths.

    Chromium records one HAR entry per network request, but the HAR writer and the
    sidecar (CDPResponseTap / RouteDocumentInterceptor) both instrument the same
    events.  For main-frame navigations the HAR writer often loses the body-capture
    race, producing an entry with ``size_response=None``, while the sidecar catches
    it at a different point and leaves a second HAR entry (or the same entry is
    simply recorded twice by Playwright on fast redirects).  The result is two rows
    in the network table for the same (url, method) — one with data, one without.

    Rule: when a (url, method) group contains entries both with and without
    ``size_response``, the without-data entries are HAR-miss duplicates.  Keep all
    with-data entries (genuine parallel fetches) and discard the without-data ones,
    propagating ``body_basename`` onto the first with-data entry if it lacks one.
    Groups where all entries have data (or all lack it) are left intact.
    """
    from collections import defaultdict

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, t in enumerate(txns):
        groups[(t.url, t.method)].append(i)

    suppressed: set[int] = set()
    basename_overrides: dict[int, str] = {}

    for indices in groups.values():
        if len(indices) <= 1:
            continue

        with_data = [j for j in indices if txns[j].size_response is not None]
        without_data = [j for j in indices if txns[j].size_response is None]

        if not (with_data and without_data):
            continue

        suppressed.update(without_data)

        # Propagate body_basename from any entry in the group to the first with-data winner.
        if txns[with_data[0]].body_basename is None:
            merged = next((txns[j].body_basename for j in indices if txns[j].body_basename), None)
            if merged:
                basename_overrides[with_data[0]] = merged

    result: list[Transaction] = []
    for i, t in enumerate(txns):
        if i in suppressed:
            continue
        if i in basename_overrides:
            t.body_basename = basename_overrides[i]
        result.append(t)

    return result


def get_transaction(run_dir: Path, index: int) -> TransactionDetail | None:
    """Return a single :class:`TransactionDetail` (full headers + initiator chain)."""
    raw = _load_raw_har(run_dir)
    entries = raw.get("log", {}).get("entries", []) if raw else []
    if index < 0 or index >= len(entries):
        return None

    entry = entries[index]
    req = entry.get("request") or {}
    resp = entry.get("response") or {}
    content = resp.get("content") or {}
    initiator = entry.get("_initiator") or {}

    url = req.get("url", "").strip()
    if not url:
        return None

    method = (req.get("method") or "GET").upper()
    status = resp.get("status")
    mime_raw = content.get("mimeType") or None
    mime = _strip_charset(mime_raw)
    resource_type = entry.get("_resourceType") or _infer_resource_type(mime_raw, url)
    server_ip = (entry.get("serverIPAddress") or "").strip("[] ") or None
    started_at = entry.get("startedDateTime") or None
    time_ms_raw = entry.get("time")
    time_ms = float(time_ms_raw) if time_ms_raw is not None else None
    size_resp = content.get("size")
    size_req = req.get("bodySize")
    if size_req is not None and size_req < 0:
        size_req = None

    itype = initiator.get("type", "other") if initiator else "other"
    iurl = _extract_initiator_url(initiator)

    resp_file = content.get("_file")
    body_basename: str | None = Path(resp_file).name if resp_file else None
    if body_basename is None:
        har_refs = map_body_files(run_dir / "har_full.har") if (run_dir / "har_full.har").exists() else {}
        sidecar_refs = load_capture_manifest(run_dir)
        all_refs = {**sidecar_refs, **har_refs}
        for bn, ref in all_refs.items():
            if ref.url == url and ref.direction == "response":
                body_basename = bn
                break

    def _headers(lst: list) -> list[tuple[str, str]]:
        return [(h.get("name", ""), h.get("value", "")) for h in lst if isinstance(h, dict)]

    req_headers = _headers(req.get("headers") or [])
    resp_headers = _headers(resp.get("headers") or [])

    frame_url: str | None = None
    stack = initiator.get("stack") or {}
    frames = stack.get("callFrames") or []
    if frames:
        frame_url = frames[0].get("url") or None

    all_txns = load_transactions(run_dir)
    initiator_chain = _build_initiator_chain(all_txns, url)

    return TransactionDetail(
        index=index,
        started_at=started_at,
        time_ms=time_ms,
        method=method,
        url=url,
        status=status,
        mime_type=mime,
        resource_type=resource_type,
        size_response=size_resp,
        size_request=size_req,
        server_ip=server_ip,
        initiator_type=itype,
        initiator_url=iurl,
        body_basename=body_basename,
        capture_outcome=None,
        is_text=_is_text_mime(mime),
        is_image=_is_image_mime(mime),
        request_headers=req_headers,
        response_headers=resp_headers,
        frame_url=frame_url,
        initiator_chain=initiator_chain,
    )


def _build_initiator_chain(txns: list[Transaction], url: str) -> list[str]:
    """Walk the reverse initiator graph from *url* up to the root."""
    child_to_parent: dict[str, str] = {}
    for t in txns:
        if t.initiator_url:
            child_to_parent[t.url] = t.initiator_url

    chain: list[str] = []
    seen: set[str] = set()
    current = child_to_parent.get(url)
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = child_to_parent.get(current)
    chain.reverse()
    return chain


def _find_body_file(run_dir: Path, basename: str) -> Path | None:
    """Resolve a body basename to its actual path under run_dir.

    Playwright's HAR attach mode writes SHA1-named files directly to the run
    root; the agent sidecar writes SHA256-named files into bodies/.  Both are
    valid locations — check bodies/ first, then fall back to the run root.
    """
    candidate = run_dir / "bodies" / basename
    if candidate.exists():
        return candidate
    candidate = run_dir / basename
    if candidate.exists():
        return candidate
    return None


def load_body_preview(
    run_dir: Path,
    basename: str,
    mime: str | None,
    max_bytes: int = 262144,
) -> BodyPreview:
    """Read a body file and return a decoded preview.

    Returns a :class:`BodyPreview` regardless of whether the file exists —
    callers check ``size_bytes == 0`` or ``text is None`` for the missing case.
    """
    file_path = _find_body_file(run_dir, basename)
    if file_path is None:
        return BodyPreview(
            mime_type=_strip_charset(mime),
            is_text=_is_text_mime(mime),
            is_image=_is_image_mime(mime),
            text=None,
            truncated=False,
            size_bytes=0,
            basename=basename,
        )

    raw = file_path.read_bytes()
    size = len(raw)
    truncated = size > max_bytes
    chunk = raw[:max_bytes]

    text: str | None = None
    if _is_text_mime(mime):
        text = chunk.decode("utf-8", errors="replace")

    return BodyPreview(
        mime_type=_strip_charset(mime),
        is_text=_is_text_mime(mime),
        is_image=_is_image_mime(mime),
        text=text,
        truncated=truncated,
        size_bytes=size,
        basename=basename,
    )


def _validate_basename(basename: str) -> bool:
    """Return True only if basename is a safe non-empty plain filename with no path components."""
    if not basename:
        return False
    p = Path(basename)
    return p.name == basename and "/" not in basename and "\\" not in basename and ".." not in basename
