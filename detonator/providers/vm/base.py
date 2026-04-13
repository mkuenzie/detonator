"""Abstract base class for VM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from detonator.models import NetworkInfo, VMInfo, VMState


class VMProvider(ABC):
    """Technology-agnostic interface for VM lifecycle management.

    Each hypervisor (Proxmox, libvirt, etc.) implements this interface.
    The orchestrator interacts only through these methods.
    """

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Initialize the provider with connection/auth details."""

    @abstractmethod
    async def list_vms(self) -> list[VMInfo]:
        """Return summary info for all VMs managed by this provider."""

    @abstractmethod
    async def get_state(self, vm_id: str) -> VMState:
        """Return the current power state of a VM."""

    @abstractmethod
    async def revert(self, vm_id: str, snapshot_id: str) -> None:
        """Revert a VM to a named snapshot.

        The VM should be stopped before reverting. Implementations may
        choose to stop it automatically or raise if it's running.
        """

    @abstractmethod
    async def start(self, vm_id: str) -> None:
        """Power on a VM."""

    @abstractmethod
    async def stop(self, vm_id: str, *, force: bool = False) -> None:
        """Shut down a VM.

        Args:
            vm_id: The VM identifier.
            force: If True, force-stop (power off) instead of graceful shutdown.
        """

    @abstractmethod
    async def get_console_url(self, vm_id: str) -> str:
        """Return a VNC/SPICE console URL for interactive access."""

    @abstractmethod
    async def get_network_info(self, vm_id: str) -> NetworkInfo:
        """Return network configuration (IP, MAC, bridge) for a VM."""
