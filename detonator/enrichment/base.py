"""Abstract base class for enrichment modules."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from detonator.models.observables import Observable, ObservableLink

# Stable namespace for deterministic observable UUIDs.
# Using uuid5(namespace, "type:value") gives the same ID for the same indicator
# across runs, which makes observable deduplication and link references reliable.
_OBS_NS = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")


def observable_id(obs_type: str, value: str) -> uuid.UUID:
    """Return a deterministic UUID for a (type, value) observable pair."""
    return uuid.uuid5(_OBS_NS, f"{obs_type}:{value.lower().strip()}")


class EnrichmentResult(BaseModel):
    """Output from a single enricher for a single input."""

    enricher: str
    input_value: str
    data: dict[str, Any] = {}
    error: str | None = None
    observables: list[Observable] = []
    observable_links: list[ObservableLink] = []


class RunContext(BaseModel):
    """Context passed to enrichers — contains paths to run artifacts."""

    run_id: str
    artifact_dir: str
    seed_url: str
    domains: list[str] = []
    ips: list[str] = []
    urls: list[str] = []


class Enricher(ABC):
    """Technology-agnostic interface for enrichment modules.

    Each enrichment source (WHOIS, DNS, TLS, favicon, etc.)
    implements this interface. The pipeline fans out to all
    enrichers whose accepts() returns True.
    """

    supports_exclusions: ClassVar[bool] = False

    def __init__(self, exclude_hosts: list[str] | None = None) -> None:
        self._exclude_hosts: set[str] = {
            h.lower().strip() for h in (exclude_hosts or []) if h.strip()
        }

    def _is_host_excluded(self, host: str) -> bool:
        host = host.lower().strip().removeprefix("www")
        if host in self._exclude_hosts:
            return True
        return any(host.endswith(f".{d}") for d in self._exclude_hosts)

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this enricher."""

    @abstractmethod
    def accepts(self, artifact_type: str) -> bool:
        """Return True if this enricher can process the given artifact type."""

    @abstractmethod
    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        """Run enrichment against the artifacts in context.

        Returns one EnrichmentResult per input processed (e.g., one per domain).
        """
