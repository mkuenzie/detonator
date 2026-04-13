"""TOML configuration loading and validation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


class AgentConfig(BaseModel):
    """Configuration for communicating with the in-VM agent."""

    port: int = 8000
    health_timeout_sec: int = 60
    health_poll_sec: int = 2


class TimeoutsConfig(BaseModel):
    """Per-stage timeout defaults."""

    provision_sec: int = 120
    preflight_sec: int = 30
    detonate_sec: int = 120
    collect_sec: int = 60
    enrich_sec: int = 120


class DetonatorConfig(BaseModel):
    """Top-level configuration for the detonator host orchestrator."""

    vm_provider: VMProviderConfig = VMProviderConfig()
    default_vm_id: str | None = None
    default_snapshot: str | None = None
    egress: dict[str, EgressConfig] = {}
    storage: StorageConfig = StorageConfig()
    agent: AgentConfig = AgentConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    enrichment_modules: list[str] = Field(
        default=["whois", "dns", "tls", "favicon"]
    )
    log_level: str = "INFO"


def load_config(path: str | Path) -> DetonatorConfig:
    """Load and validate a TOML config file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return DetonatorConfig(**raw)
