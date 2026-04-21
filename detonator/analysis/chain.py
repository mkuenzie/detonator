"""HAR chain extraction — builds the initiator graph and walks the seed URL chain.

Chromium's HAR format includes a ``_initiator`` field on every entry that
records what caused the request.  This module uses those relationships to build
a directed graph (initiator → target) and walks it from the seed URL outward,
separating the "meaningful" chain from unrelated background noise.

Typical flow::

    entries = parse_har(har_path)
    result  = extract_chain(har_path, seed_url)
    # result.chain_entries  → requests traceable to the seed URL
    # result.noise_entries  → requests with no path to the seed
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Data models ──────────────────────────────────────────────────────


class HarEntry(BaseModel):
    """Parsed representation of a single HAR log entry."""

    url: str
    method: str = "GET"
    resource_type: str = "other"    # Chromium ``_resourceType``
    initiator_type: str = "other"   # Chromium ``_initiator.type``
    initiator_url: str | None = None
    server_ip: str | None = None
    response_status: int | None = None
    mime_type: str | None = None


class ChainResult(BaseModel):
    """Output from :func:`extract_chain`."""

    seed_url: str
    chain_urls: list[str]           # deduped, order-of-first-appearance
    noise_urls: list[str]
    all_entries: list[HarEntry]
    chain_entries: list[HarEntry]
    noise_entries: list[HarEntry]
    har_chain: dict                 # minimal HAR dict (chain entries only)
    har_all: dict = {}              # full raw HAR (all entries) — used by filter to include orphans


# ── HAR parsing ──────────────────────────────────────────────────────


def _extract_initiator_url(initiator: dict) -> str | None:
    """Return the best-effort parent URL from a Chromium ``_initiator`` object."""
    if not initiator:
        return None
    itype = initiator.get("type", "other")

    if itype in ("redirect", "parser", "preload"):
        return initiator.get("url")

    if itype == "script":
        stack = initiator.get("stack", {})
        for frame_source in (stack, stack.get("parent", {})):
            frames = frame_source.get("callFrames", [])
            if frames:
                url = frames[0].get("url", "")
                if url and not url.startswith("chrome-extension://"):
                    return url
    return None


def parse_har(har_path: Path) -> list[HarEntry]:
    """Parse *har_path* and return one :class:`HarEntry` per request.

    Returns an empty list if the file is missing or malformed.
    """
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse HAR at %s: %s", har_path, exc)
        return []

    result: list[HarEntry] = []
    for raw in data.get("log", {}).get("entries", []):
        req = raw.get("request", {})
        resp = raw.get("response", {})
        initiator = raw.get("_initiator") or {}

        url = req.get("url", "").strip()
        if not url:
            continue

        server_ip_raw = raw.get("serverIPAddress", "").strip("[] ")
        status = resp.get("status")
        mime = resp.get("content", {}).get("mimeType", "") or None
        itype = initiator.get("type", "other") if initiator else "other"

        result.append(
            HarEntry(
                url=url,
                method=req.get("method", "GET"),
                resource_type=raw.get("_resourceType", "other"),
                initiator_type=itype,
                initiator_url=_extract_initiator_url(initiator),
                server_ip=server_ip_raw or None,
                response_status=status,
                mime_type=mime,
            )
        )
    return result


# ── Graph & chain walk ───────────────────────────────────────────────


def build_initiator_graph(entries: list[HarEntry]) -> dict[str, list[str]]:
    """Return a forward adjacency map: parent_url → [child_urls].

    Used by :func:`walk_chain` to BFS from the seed URL.
    """
    graph: dict[str, list[str]] = {}
    for entry in entries:
        if entry.initiator_url:
            graph.setdefault(entry.initiator_url, []).append(entry.url)
    return graph


def _best_seed_url(entries: list[HarEntry], seed_url: str) -> str:
    """Find the URL in *entries* that best represents the seed URL.

    Tries exact match first, then normalised (no fragment), then falls back
    to the first entry in the HAR (Playwright always records the navigation
    first).
    """
    # Exact match
    for e in entries:
        if e.url == seed_url:
            return e.url

    # Normalised match (strip fragment/trailing slash differences)
    def _norm(u: str) -> str:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme}://{p.netloc}{path}"

    seed_norm = _norm(seed_url)
    for e in entries:
        if _norm(e.url) == seed_norm:
            return e.url

    # Fallback: first entry
    return entries[0].url if entries else seed_url


def walk_chain(entries: list[HarEntry], seed_url: str) -> list[str]:
    """BFS from *seed_url* following forward initiator edges.

    Returns the ordered list of URLs reachable from the seed via
    ``_initiator`` relationships (preserves order of first discovery).
    """
    if not entries:
        return []

    graph = build_initiator_graph(entries)
    start = _best_seed_url(entries, seed_url)

    chain_ordered: list[str] = []
    seen: set[str] = set()
    queue: list[str] = [start]

    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        chain_ordered.append(current)
        for child in graph.get(current, []):
            if child not in seen:
                queue.append(child)

    return chain_ordered


# ── Top-level extractor ──────────────────────────────────────────────


def extract_chain(har_path: Path, seed_url: str) -> ChainResult | None:
    """Parse *har_path*, build the initiator graph, and walk the chain.

    Returns ``None`` when the file is missing or empty.  The returned
    :class:`ChainResult` has ``chain_entries`` (in chain) and
    ``noise_entries`` (no path to seed).  ``har_chain`` is the minimal
    HAR dict ready to serialise as ``har_chain.json``.
    """
    entries = parse_har(har_path)
    if not entries:
        return None

    chain_urls = walk_chain(entries, seed_url)
    chain_url_set = set(chain_urls)

    chain_entries = [e for e in entries if e.url in chain_url_set]
    noise_entries = [e for e in entries if e.url not in chain_url_set]

    # Preserve the original raw entries for the filtered HAR
    try:
        raw_data: dict = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception:
        raw_data = {"log": {"version": "1.2", "entries": []}}

    raw_entries = raw_data.get("log", {}).get("entries", [])
    filtered_raw = [
        e for e in raw_entries
        if e.get("request", {}).get("url", "") in chain_url_set
    ]
    log_section = {**raw_data.get("log", {}), "entries": filtered_raw}
    har_chain = {**raw_data, "log": log_section}

    return ChainResult(
        seed_url=seed_url,
        chain_urls=chain_urls,
        noise_urls=[e.url for e in noise_entries],
        all_entries=entries,
        chain_entries=chain_entries,
        noise_entries=noise_entries,
        har_chain=har_chain,
        har_all=raw_data,
    )
