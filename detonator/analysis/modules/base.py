"""Abstract base for analysis modules and shared data models."""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from detonator.analysis.filter import FilterResult
from detonator.analysis.navigation import HarEntry, NavigationScope

# Stable namespace for deterministic technique UUIDs — matches the value in
# filter.py so existing DB rows keep the same IDs after the migration.
_TECH_NS = uuid.UUID("b4c0ffee-dead-beef-cafe-000000000001")


def _tech_id(name: str) -> str:
    """Deterministic UUID for a technique name — same ID across runs."""
    return str(uuid.uuid5(_TECH_NS, name))


_TEXTY_MIMES = frozenset({
    "text/html", "text/javascript", "application/javascript",
    "application/json", "text/plain", "text/css",
    "application/x-javascript", "text/x-javascript",
})
_MAX_BODY = 2 * 1024 * 1024  # 2 MiB


class ResourceContent(BaseModel):
    """A text-like site_resource body captured during detonation."""

    url: str
    host: str
    mime_type: str
    size_bytes: int
    body: str  # utf-8, errors="replace", truncated to _MAX_BODY


class TechniqueHit(BaseModel):
    """A matched technique returned by an AnalysisModule."""

    technique_id: str        # deterministic UUID string
    name: str
    description: str
    signature_type: str      # SignatureType value
    confidence: float
    evidence: dict
    detection_module: str    # which module produced this hit


class AnalysisContext(BaseModel):
    """Derived facts computed from navigation-scope + filter results, passed to modules."""

    run_id: str
    seed_url: str
    seed_hostname: str

    # Post-noise entries from the filter pass
    navigation_entries: list[HarEntry] = []
    navigation_hostnames: list[str] = []
    navigation_urls: list[str] = []
    navigation_initiator_types: list[str] = []
    navigation_resource_types: list[str] = []

    # Redirect sub-graph
    redirect_domains: list[str] = []
    cross_origin_redirect_count: int = 0

    # Optional artifact content (None when file absent)
    dom_html: str | None = None

    # Placeholder — future module fills this via DOM analysis
    js_writes_location: bool | None = None

    # Text-like site_resource bodies (populated when artifacts kwarg is provided)
    resources: list[ResourceContent] = []

    @classmethod
    def from_navigation_scope(
        cls,
        nav_scope: NavigationScope,
        noise_filter_result: FilterResult,
        artifact_dir: str,
        run_id: str,
        seed_url: str,
        artifacts: list[dict] | None = None,
    ) -> AnalysisContext:
        """Build context from navigation-scope + noise filter results."""
        clean_urls: set[str] = {
            e.url for e in noise_filter_result.entries if not e.is_noise
        }
        clean_entries = [e for e in nav_scope.all_entries if e.url in clean_urls]

        def _netloc(url: str) -> str:
            return urlparse(url).netloc or ""

        nav_hostnames = list(dict.fromkeys(_netloc(e.url) for e in clean_entries if _netloc(e.url)))
        nav_urls = [e.url for e in clean_entries]
        nav_initiator_types = list(dict.fromkeys(e.initiator_type for e in clean_entries))
        nav_resource_types = list(dict.fromkeys(e.resource_type for e in clean_entries))

        redirect_entries = [e for e in clean_entries if e.initiator_type == "redirect"]
        redirect_domains = sorted({_netloc(e.url) for e in redirect_entries if _netloc(e.url)})
        cross_origin_redirect_count = len(redirect_domains)

        dom_html: str | None = None
        dom_path = Path(artifact_dir) / "dom.html"
        if dom_path.exists():
            try:
                dom_html = dom_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        resources = _load_resources(artifact_dir, artifacts or [])

        return cls(
            run_id=run_id,
            seed_url=seed_url,
            seed_hostname=urlparse(seed_url).hostname or "",
            navigation_entries=clean_entries,
            navigation_hostnames=nav_hostnames,
            navigation_urls=nav_urls,
            navigation_initiator_types=nav_initiator_types,
            navigation_resource_types=nav_resource_types,
            redirect_domains=redirect_domains,
            cross_origin_redirect_count=cross_origin_redirect_count,
            dom_html=dom_html,
            resources=resources,
        )


def _build_url_mime_map(artifact_dir: str) -> dict[str, str]:
    """Return {url: mime_type} from har_full.har response content MIME types."""
    har_path = Path(artifact_dir) / "har_full.har"
    if not har_path.exists():
        return {}
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
        return {
            e.get("request", {}).get("url", ""): (
                e.get("response", {}).get("content", {}).get("mimeType", "") or ""
            ).split(";")[0].strip().lower()
            for e in data.get("log", {}).get("entries", [])
            if e.get("request", {}).get("url")
        }
    except Exception:
        return {}


def _load_resources(artifact_dir: str, artifacts: list[dict]) -> list[ResourceContent]:
    """Load text-like site_resource bodies under the 2 MiB cap."""
    url_to_mime = _build_url_mime_map(artifact_dir)
    resources: list[ResourceContent] = []
    for a in artifacts:
        if a.get("artifact_type") != "site_resource":
            continue
        src_url = a.get("source_url") or ""
        mime = url_to_mime.get(src_url, "")
        if not mime:
            continue
        if mime not in _TEXTY_MIMES:
            continue
        size = a.get("size") or 0
        if size > _MAX_BODY:
            continue
        path_str = a.get("path") or ""
        if not path_str:
            continue
        try:
            body = Path(path_str).read_text(encoding="utf-8", errors="replace")[:_MAX_BODY]
        except OSError:
            continue
        resources.append(
            ResourceContent(
                url=src_url,
                host=urlparse(src_url).netloc or "",
                mime_type=mime,
                size_bytes=size,
                body=body,
            )
        )
    return resources


class AnalysisModule(ABC):
    """Technology-agnostic interface for analysis modules.

    Each module (builtin, sigma, future ML-based, …) implements this interface.
    The pipeline fans out to all modules and aggregates their hits.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this module."""

    @abstractmethod
    async def analyze(self, context: AnalysisContext) -> list[TechniqueHit]:
        """Run analysis against *context* and return zero or more technique hits."""
