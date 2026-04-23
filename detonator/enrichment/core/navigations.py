"""Navigation enricher — emits ``redirects_to`` observable links from framenavigated events.

Reads ``navigations.json`` (written by the Playwright agent) and creates
``DOMAIN → DOMAIN`` edges in the observable graph for each cross-host
main-frame navigation.

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


def _build_url_trigger_map(har_path: Path) -> dict[str, str]:
    """Return {url: trigger} by inspecting HAR entries.

    trigger is "redirect" for 30x responses, "script" for script-initiated
    requests, "unknown" otherwise.
    """
    mapping: dict[str, str] = {}
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
        for entry in data.get("log", {}).get("entries", []):
            url = entry.get("request", {}).get("url", "")
            if not url:
                continue
            status = entry.get("response", {}).get("status", 0)
            initiator_type = entry.get("_initiator", {}).get("type", "")
            if 300 <= status < 400:
                mapping[url] = "redirect"
            elif initiator_type == "script":
                mapping.setdefault(url, "script")
            else:
                mapping.setdefault(url, "unknown")
    except Exception as exc:
        logger.warning("navigations: could not read har_full.har: %s", exc)
    return mapping


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

        # Load HAR trigger map once (best-effort)
        har_path = Path(context.artifact_dir) / "har_full.har"
        trigger_map = _build_url_trigger_map(har_path) if har_path.exists() else {}

        # Filter to main-frame only, then deduplicate consecutive identical URLs
        main_frames = [n for n in navigations if n.get("frame") == "main"]
        deduped: list[dict] = []
        for nav in main_frames:
            if not deduped or nav["url"] != deduped[-1]["url"]:
                deduped.append(nav)

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
