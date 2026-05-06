"""Navigation enricher — emits ``redirects_to`` observable links from framenavigated events.

Reads ``navigations.json`` (written by the Playwright agent) and creates
``DOMAIN → DOMAIN`` edges in the observable graph for each cross-host
main-frame navigation.

Playwright's ``framenavigated`` event only fires on *committed* navigations —
HTTP 302 hops are resolved at the network layer before any document is
committed, so intermediate redirect targets are absent from ``navigations.json``.
This enricher compensates by cross-referencing ``har_full.har`` to expand
those gaps: between any two consecutive navigation entries it follows the HAR's
3xx redirect chain and inserts synthetic intermediate hops.

Trigger classification cross-references ``har_full.har``:
- 30x response status → ``"redirect"``
- ``_initiator.type == "script"`` → ``"script"``
- otherwise → ``"unknown"``
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import (
    Observable,
    ObservableLink,
    ObservableType,
    RelationshipType,
)

logger = logging.getLogger(__name__)


def _hostname(url: str) -> str:
    return urlparse(url).hostname or ""


def _parse_har_entries(har_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Parse HAR and return (trigger_map, redirect_map).

    trigger_map: {url → trigger}  where trigger is "redirect" | "script" | "unknown"
    redirect_map: {url → location}  for every 3xx response that has a Location header
    """
    trigger_map: dict[str, str] = {}
    redirect_map: dict[str, str] = {}
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
        for entry in data.get("log", {}).get("entries", []):
            url = entry.get("request", {}).get("url", "")
            if not url:
                continue
            status = entry.get("response", {}).get("status", 0)
            initiator_type = entry.get("_initiator", {}).get("type", "")
            if 300 <= status < 400:
                trigger_map[url] = "redirect"
                for h in entry.get("response", {}).get("headers", []):
                    if h.get("name", "").lower() == "location":
                        redirect_map[url] = h["value"]
                        break
            elif initiator_type == "script":
                trigger_map.setdefault(url, "script")
            else:
                trigger_map.setdefault(url, "unknown")
    except Exception as exc:
        logger.warning("navigations: could not read har_full.har: %s", exc)
    return trigger_map, redirect_map


def _expand_redirects(
    navigations: list[dict],
    redirect_map: dict[str, str],
    max_hops: int = 20,
) -> list[dict]:
    """Insert intermediate HTTP redirect hops between consecutive navigation entries.

    Playwright's framenavigated fires only on committed navigations, so HTTP
    3xx intermediates are absent from navigations.json.  This function walks
    the redirect_map (built from HAR 3xx entries) forward from each navigation
    URL, collecting hops until it either reaches the next committed URL or runs
    out of known redirects, then splices them in as synthetic entries.
    """
    if not redirect_map:
        return navigations

    expanded: list[dict] = []
    for i, nav in enumerate(navigations):
        expanded.append(nav)
        if i + 1 >= len(navigations):
            continue

        next_url = navigations[i + 1]["url"]
        intermediates: list[str] = []
        seen: set[str] = {nav["url"]}
        current = nav["url"]

        for _ in range(max_hops):
            target = redirect_map.get(current)
            if not target or target in seen:
                break
            seen.add(target)
            if target == next_url:
                break
            intermediates.append(target)
            current = target

        ts = nav.get("timestamp", "")
        for mid_url in intermediates:
            expanded.append({"timestamp": ts, "url": mid_url, "frame": "main", "via": "http_redirect"})

    return expanded


class NavigationEnricher(Enricher):
    """Emits ``redirects_to`` observable links from main-frame navigation events."""

    def __init__(self) -> None:
        super().__init__(exclude_hosts=None)

    @property
    def name(self) -> str:
        return "navigations"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "navigations"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        nav_path = Path(context.artifact_dir) / "navigations.json"
        if not nav_path.exists():
            return [EnrichmentResult(enricher=self.name, input_value="", error="navigations.json not found")]

        try:
            navigations: list[dict] = json.loads(nav_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [EnrichmentResult(enricher=self.name, input_value="", error=str(exc))]

        # Load HAR once (best-effort) for trigger classification and redirect expansion
        har_path = Path(context.artifact_dir) / "har_full.har"
        if har_path.exists():
            trigger_map, redirect_map = _parse_har_entries(har_path)
        else:
            trigger_map, redirect_map = {}, {}

        # Filter to main-frame only, then deduplicate consecutive identical URLs
        main_frames = [n for n in navigations if n.get("frame") == "main"]
        deduped: list[dict] = []
        for nav in main_frames:
            if not deduped or nav["url"] != deduped[-1]["url"]:
                deduped.append(nav)

        # Splice in HTTP 302 intermediates that framenavigated never sees
        deduped = _expand_redirects(deduped, redirect_map)

        now = datetime.now(UTC)
        observables: list[Observable] = []
        links: list[ObservableLink] = []
        seen_obs: set[str] = set()

        def _upsert_domain(host: str) -> Observable:
            obs_id = observable_id(ObservableType.DOMAIN, host)
            key = str(obs_id)
            obs = Observable(
                id=obs_id,
                type=ObservableType.DOMAIN,
                value=host,
                first_seen=now,
                last_seen=now,
            )
            if key not in seen_obs:
                seen_obs.add(key)
                observables.append(obs)
            return obs

        for prev_nav, next_nav in zip(deduped, deduped[1:]):
            prev_host = _hostname(prev_nav["url"])
            next_host = _hostname(next_nav["url"])
            if not prev_host or not next_host or prev_host == next_host:
                continue

            src_obs = _upsert_domain(prev_host)
            dst_obs = _upsert_domain(next_host)
            # Synthetic entries inserted by _expand_redirects are always HTTP redirects
            if next_nav.get("via") == "http_redirect":
                trigger = "redirect"
            else:
                trigger = trigger_map.get(next_nav["url"], "unknown")

            links.append(
                ObservableLink(
                    source_id=src_obs.id,
                    target_id=dst_obs.id,
                    relationship=RelationshipType.REDIRECTS_TO,
                    first_seen=now,
                    last_seen=now,
                    evidence={
                        "prev_url": prev_nav["url"],
                        "next_url": next_nav["url"],
                        "timestamp": next_nav.get("timestamp", ""),
                        "trigger": trigger,
                    },
                )
            )

        logger.info(
            "run=%s navigations: %d cross-host hop(s) → %d link(s)",
            context.run_id, len(links), len(links),
        )

        return [
            EnrichmentResult(
                enricher=self.name,
                input_value=str(nav_path),
                observables=observables,
                observable_links=links,
            )
        ]
