"""Navigation-scope extraction.

The analysis pipeline is rooted at the frame navigations the browser actually
performed (captured in ``navigations.json`` by the in-VM agent), not at the
HAR initiator graph.  Rationale: JS-driven main-frame transitions
(``location.href = ...``, ``location.replace(...)``, JS meta-refresh) produce
HAR entries with no initiator parent, so a single-seed BFS from the seed URL
orphans them.

This module takes every navigation URL as a BFS root in the initiator graph
and returns the union of reachable URLs (``scope_urls``).  Subresources keep
their initiator edges back to the navigated document, so third-party CDN JS
pulled by a navigated page still lands in scope.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Literal
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


class NavigationEvent(BaseModel):
    """One frame navigation emitted by the agent."""

    timestamp: str
    url: str
    frame: Literal["main", "sub"] = "main"


class NavigationScope(BaseModel):
    """Output from :func:`extract_navigation_scope`."""

    seed_url: str
    navigation_events: list[NavigationEvent]
    navigation_urls: list[str]       # URLs from navigations.json, order preserved
    navigation_hosts: list[str]
    scope_urls: list[str]            # BFS union from all nav roots, deduped
    out_of_scope_urls: list[str]
    all_entries: list[HarEntry]
    scope_entries: list[HarEntry]
    out_of_scope_entries: list[HarEntry]
    har_full: dict = {}              # full raw HAR (all entries)
    har_navigation: dict             # filtered HAR (scope entries only)


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
    """Parse *har_path* and return one :class:`HarEntry` per request."""
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


# ── Graph & walk ─────────────────────────────────────────────────────


def build_initiator_graph(entries: list[HarEntry]) -> dict[str, list[str]]:
    """Return a forward adjacency map: parent_url → [child_urls]."""
    graph: dict[str, list[str]] = {}
    for entry in entries:
        if entry.initiator_url:
            graph.setdefault(entry.initiator_url, []).append(entry.url)
    return graph


def _resolve_root(entries: list[HarEntry], root_url: str) -> str | None:
    """Map *root_url* onto a URL that actually appears in *entries*.

    Tries exact match, then match after stripping fragment / trailing slash.
    Returns ``None`` when no match exists — the caller decides whether to
    keep the unresolved root as a BFS start (harmless if the graph has no
    edges from it) or drop it.
    """
    for e in entries:
        if e.url == root_url:
            return e.url

    def _norm(u: str) -> str:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme}://{p.netloc}{path}"

    target = _norm(root_url)
    for e in entries:
        if _norm(e.url) == target:
            return e.url
    return None


def walk_from_roots(
    entries: list[HarEntry],
    roots: Iterable[str],
) -> list[str]:
    """BFS from every URL in *roots* following forward initiator edges.

    Returns the deduped, order-of-first-discovery list of reachable URLs.
    Roots are first resolved against *entries* (exact or normalised URL
    match).  Unresolved roots are still used as BFS starts so their graph
    children are picked up — a root URL with no matching entry and no
    outgoing edge still lands in scope so ``navigations.json`` entries that
    never produced a HAR record are visible downstream.
    """
    if not entries:
        return []

    graph = build_initiator_graph(entries)

    ordered: list[str] = []
    seen: set[str] = set()

    for root in roots:
        start = _resolve_root(entries, root) or root
        queue: list[str] = [start]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            for child in graph.get(current, []):
                if child not in seen:
                    queue.append(child)
    return ordered


# ── navigations.json loader ──────────────────────────────────────────


def load_navigation_events(navigations_path: Path) -> list[NavigationEvent]:
    """Parse ``navigations.json`` produced by the in-VM agent.

    Returns an empty list when the file is missing or malformed.
    """
    try:
        data = json.loads(navigations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse navigations at %s: %s", navigations_path, exc)
        return []

    if not isinstance(data, list):
        return []

    events: list[NavigationEvent] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        frame = raw.get("frame", "main")
        if frame not in ("main", "sub"):
            frame = "main"
        events.append(
            NavigationEvent(
                timestamp=str(raw.get("timestamp", "")),
                url=url,
                frame=frame,
            )
        )
    return events


# ── Top-level extractor ──────────────────────────────────────────────


def extract_navigation_scope(
    har_path: Path,
    navigations_path: Path | None,
    seed_url: str,
) -> NavigationScope | None:
    """Build the navigation scope from a HAR and a navigations.json.

    *navigations_path* may be ``None`` or missing — in that case the seed URL
    alone is used as the BFS root (matches the legacy single-seed behavior).

    Returns ``None`` when the HAR is missing or empty.
    """
    entries = parse_har(har_path)
    if not entries:
        return None

    events: list[NavigationEvent] = []
    if navigations_path is not None and navigations_path.exists():
        events = load_navigation_events(navigations_path)

    nav_urls: list[str] = []
    seen_urls: set[str] = set()
    for ev in events:
        if ev.url not in seen_urls:
            seen_urls.add(ev.url)
            nav_urls.append(ev.url)

    # Always union the seed URL into roots as a safety net.
    roots = list(nav_urls)
    if seed_url and seed_url not in seen_urls:
        roots.append(seed_url)

    if not roots:
        roots = [seed_url] if seed_url else []

    scope_urls = walk_from_roots(entries, roots)
    scope_set = set(scope_urls)

    scope_entries = [e for e in entries if e.url in scope_set]
    out_of_scope_entries = [e for e in entries if e.url not in scope_set]

    # Build the filtered HAR using the raw entries (preserves all HAR fields).
    try:
        raw_data: dict = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception:
        raw_data = {"log": {"version": "1.2", "entries": []}}

    raw_entries = raw_data.get("log", {}).get("entries", [])
    filtered_raw = [
        e for e in raw_entries
        if e.get("request", {}).get("url", "") in scope_set
    ]
    log_section = {**raw_data.get("log", {}), "entries": filtered_raw}
    har_navigation = {**raw_data, "log": log_section}

    nav_hosts = list(dict.fromkeys(
        urlparse(u).netloc for u in nav_urls if urlparse(u).netloc
    ))

    return NavigationScope(
        seed_url=seed_url,
        navigation_events=events,
        navigation_urls=nav_urls,
        navigation_hosts=nav_hosts,
        scope_urls=scope_urls,
        out_of_scope_urls=[e.url for e in out_of_scope_entries],
        all_entries=entries,
        scope_entries=scope_entries,
        out_of_scope_entries=out_of_scope_entries,
        har_full=raw_data,
        har_navigation=har_navigation,
    )
