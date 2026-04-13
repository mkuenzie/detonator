"""Data models for VM provider abstraction."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class VMState(StrEnum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class NetworkInfo(BaseModel):
    """Network configuration reported by a VM provider."""

    ip_address: str | None = None
    mac_address: str | None = None
    bridge: str | None = None
    vlan_tag: int | None = None


class VMInfo(BaseModel):
    """Summary of a VM as reported by the provider."""

    vm_id: str
    name: str
    state: VMState
    snapshots: list[str] = []
    network: NetworkInfo | None = None
