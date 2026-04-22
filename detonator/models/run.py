"""Data models for detonation runs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class RunState(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    PREFLIGHT = "preflight"
    DETONATING = "detonating"
    INTERACTIVE = "interactive"
    COLLECTING = "collecting"
    ENRICHING = "enriching"
    FILTERING = "filtering"
    COMPLETE = "complete"
    ERROR = "error"


class EgressType(StrEnum):
    DIRECT = "direct"
    VPN = "vpn"
    TETHER = "tether"


class ArtifactType(StrEnum):
    HAR_FULL = "har_full"
    HAR_CHAIN = "har_chain"
    SCREENSHOT = "screenshot"
    DOM = "dom"
    CONSOLE = "console"
    NAVIGATIONS = "navigations"
    SITE_RESOURCE = "site_resource"
    REQUEST_BODY = "request_body"
    FILTER_RESULT = "filter_result"
    META = "meta"
    ENRICHMENT_WHOIS = "enrichment_whois"
    ENRICHMENT_DNS = "enrichment_dns"
    ENRICHMENT_TLS = "enrichment_tls"
    ENRICHMENT_FAVICON = "enrichment_favicon"
    MANIFEST = "manifest"


class RunConfig(BaseModel):
    """Configuration for a single detonation run."""

    url: str
    egress: EgressType = EgressType.DIRECT
    timeout_sec: int = 60
    interactive: bool = False
    vm_id: str | None = None
    snapshot_id: str | None = None
    screenshot_interval_sec: int | None = None


class StateTransition(BaseModel):
    """Record of a run state change."""

    from_state: RunState
    to_state: RunState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None


class RunRecord(BaseModel):
    """Persistent record of a detonation run."""

    id: UUID = Field(default_factory=uuid4)
    config: RunConfig
    state: RunState = RunState.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    error: str | None = None
    artifact_dir: str | None = None
    transitions: list[StateTransition] = []
