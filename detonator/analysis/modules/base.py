"""Abstract base for analysis modules and shared data models."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from detonator.analysis.chain import ChainResult, HarEntry
from detonator.analysis.filter import FilterResult

# Stable namespace for deterministic technique UUIDs — matches the value in
# filter.py so existing DB rows keep the same IDs after the migration.
_TECH_NS = uuid.UUID("b4c0ffee-dead-beef-cafe-000000000001")


def _tech_id(name: str) -> str:
    """Deterministic UUID for a technique name — same ID across runs."""
    return str(uuid.uuid5(_TECH_NS, name))


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
    """Derived facts computed from chain + filter results, passed to modules."""

    run_id: str
    seed_url: str
    seed_hostname: str

    # Post-noise entries from the filter pass
    chain_entries: list[HarEntry] = []
    chain_hostnames: list[str] = []
    chain_urls: list[str] = []
    chain_initiator_types: list[str] = []
    chain_resource_types: list[str] = []

    # Redirect sub-graph
    redirect_domains: list[str] = []
    cross_origin_redirect_count: int = 0

    # Optional artifact content (None when file absent)
    dom_html: str | None = None

    # Placeholder — future module fills this via DOM analysis
    js_writes_location: bool | None = None

    @classmethod
    def from_chain(
        cls,
        chain_result: ChainResult,
        noise_filter_result: FilterResult,
        artifact_dir: str,
        run_id: str,
        seed_url: str,
    ) -> AnalysisContext:
        """Build context from chain extraction + noise filter results."""
        clean_urls: set[str] = {
            e.url for e in noise_filter_result.entries if not e.is_noise
        }
        clean_entries = [e for e in chain_result.all_entries if e.url in clean_urls]

        def _netloc(url: str) -> str:
            return urlparse(url).netloc or ""

        chain_hostnames = list(dict.fromkeys(_netloc(e.url) for e in clean_entries if _netloc(e.url)))
        chain_urls = [e.url for e in clean_entries]
        chain_initiator_types = list(dict.fromkeys(e.initiator_type for e in clean_entries))
        chain_resource_types = list(dict.fromkeys(e.resource_type for e in clean_entries))

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

        return cls(
            run_id=run_id,
            seed_url=seed_url,
            seed_hostname=urlparse(seed_url).hostname or "",
            chain_entries=clean_entries,
            chain_hostnames=chain_hostnames,
            chain_urls=chain_urls,
            chain_initiator_types=chain_initiator_types,
            chain_resource_types=chain_resource_types,
            redirect_domains=redirect_domains,
            cross_origin_redirect_count=cross_origin_redirect_count,
            dom_html=dom_html,
        )


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
