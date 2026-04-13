"""Abstract base class for egress providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class PreflightResult(BaseModel):
    """Result of an egress isolation pre-flight check."""

    passed: bool
    public_ip: str | None = None
    dns_ok: bool = False
    lan_isolated: bool = False
    details: list[str] = []


class EgressProvider(ABC):
    """Technology-agnostic interface for egress path management.

    Each egress type (direct, VPN, USB tether) implements this interface.
    The orchestrator calls activate() before detonation and deactivate() after.
    """

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Initialize the provider with network/routing details."""

    @abstractmethod
    async def activate(self, vm_id: str) -> None:
        """Apply routing and firewall rules for this egress path.

        After this call, the VM's traffic should only be able to
        reach the internet through the designated egress.
        """

    @abstractmethod
    async def deactivate(self, vm_id: str) -> None:
        """Tear down routing and firewall rules.

        Must be idempotent — safe to call even if activate() was never called.
        """

    @abstractmethod
    async def preflight_check(self, vm_id: str) -> PreflightResult:
        """Verify that egress isolation is working correctly.

        Checks:
        - DNS resolution goes through the expected path
        - Public IP matches the expected egress
        - Host LAN is unreachable from the VM
        """

    @abstractmethod
    async def get_public_ip(self) -> str:
        """Return the public IP address this egress path will use."""
