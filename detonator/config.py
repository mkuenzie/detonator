"""TOML configuration loading and validation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class VMProviderConfig(BaseModel):
    """Configuration for the VM provider."""

    type: str = "proxmox"
    settings: dict[str, Any] = {}


class EgressConfig(BaseModel):
    """Configuration for a single egress option."""

    type: str
    settings: dict[str, Any] = {}


class StorageConfig(BaseModel):
    """Configuration for the storage layer."""

    data_dir: str = "data"
    db_path: str = "data/detonator.db"


class StealthConfig(BaseModel):
    """Fingerprint-hardening settings forwarded to the in-VM agent.

    Mirrors ``agent.browser.base.StealthProfile`` — kept separate so the
    host package has no import dependency on the agent package.
    """

    enabled: bool = True
    user_agent: str | None = None
    locale: str = "en-US"
    timezone_id: str = "America/Los_Angeles"
    viewport_width: int = 1920
    viewport_height: int = 1080
    geolocation_lat: float = 45.5051   # Portland, OR
    geolocation_lon: float = -122.6750


class AgentInstanceConfig(BaseModel):
    """A named agent — pairs a VM + snapshot with the agent HTTP endpoint."""

    name: str
    vm_id: str
    snapshot: str
    port: int = 8000
    health_timeout_sec: int = 60
    health_poll_sec: int = 2
    stealth: StealthConfig = StealthConfig()


class TimeoutsConfig(BaseModel):
    """Per-stage timeout defaults."""

    provision_sec: int = 120
    preflight_sec: int = 30
    detonate_sec: int = 120
    collect_sec: int = 60
    enrich_sec: int = 120
    filter_sec: int = 30


class FilterConfig(BaseModel):
    """Configuration for the navigation-scope filter and technique detector."""

    # Additional noise domains beyond the built-in defaults.
    noise_domains: list[str] = []
    # Additional resource types to classify as noise (supplements built-ins).
    noise_resource_types: list[str] = []
    # When False (default) out-of-scope entries (not reachable from any
    # navigation root via the initiator graph) are retained in har_navigation.json
    # as long as they pass noise checks.  Set True to restrict the output strictly
    # to scope-reachable URLs.  Name kept for TOML backward-compat.
    require_initiator_chain: bool = False


class EnrichmentConfig(BaseModel):
    """Plug-in enrichers to enable. Core enrichers (navigations, dom) always run."""

    model_config = ConfigDict(extra="ignore")

    modules: list[str] = Field(default=["whois", "dns", "tls", "favicon"])


class AnalysisModuleConfig(BaseModel):
    """Placeholder for future per-module analysis settings."""


class AnalysisConfig(BaseModel):
    """Analysis pipeline configuration."""

    modules: list[str] = ["builtin", "sigma"]
    # Directories scanned for Sigma YAML rules. Paths relative to CWD or absolute.
    rules_dirs: list[str] = ["detonator/analysis/rules/builtin"]


class DetonatorConfig(BaseModel):
    """Top-level configuration for the detonator host orchestrator."""

    model_config = ConfigDict(extra="forbid")

    vm_provider: VMProviderConfig = VMProviderConfig()
    agents: list[AgentInstanceConfig] = []
    egress: dict[str, EgressConfig] = {}
    storage: StorageConfig = StorageConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    filter: FilterConfig = FilterConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    log_level: str = "INFO"

    def get_agent(self, name: str) -> AgentInstanceConfig:
        """Look up an agent by name; raises KeyError if not found."""
        for agent in self.agents:
            if agent.name == name:
                return agent
        raise KeyError(f"No agent named {name!r} in config")

    def default_agent(self) -> AgentInstanceConfig:
        """Return the first configured agent; raises if none configured."""
        if not self.agents:
            raise RuntimeError("No agents configured — add an [[agents]] entry to config")
        return self.agents[0]


def load_config(path: str | Path) -> DetonatorConfig:
    """Load and validate a TOML config file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return DetonatorConfig(**raw)
