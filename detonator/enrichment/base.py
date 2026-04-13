"""Abstract base class for enrichment modules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class EnrichmentResult(BaseModel):
    """Output from a single enricher for a single input."""

    enricher: str
    input_value: str
    data: dict[str, Any] = {}
    error: str | None = None


class RunContext(BaseModel):
    """Context passed to enrichers — contains paths to run artifacts."""

    run_id: str
    artifact_dir: str
    seed_url: str
    domains: list[str] = []
    urls: list[str] = []


class Enricher(ABC):
    """Technology-agnostic interface for enrichment modules.

    Each enrichment source (WHOIS, DNS, TLS, favicon, etc.)
    implements this interface. The pipeline fans out to all
    enrichers whose accepts() returns True.
    """

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
