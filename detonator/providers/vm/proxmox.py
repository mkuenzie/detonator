"""Proxmox VE implementation of the VMProvider interface."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from detonator.models import NetworkInfo, VMInfo, VMState
from detonator.providers.vm.base import VMProvider

logger = logging.getLogger(__name__)

# Proxmox QEMU status strings -> our VMState enum
_STATUS_MAP: dict[str, VMState] = {
    "running": VMState.RUNNING,
    "stopped": VMState.STOPPED,
    "paused": VMState.PAUSED,
}


class ProxmoxProvider(VMProvider):
    """VM lifecycle management via the Proxmox VE REST API.

    Uses the ``proxmoxer`` library for API access.  Authenticate with
    either an API token (recommended) or user/password.

    Expected config keys::

        {
            "host": "192.168.1.10",
            "port": 8006,           # optional, default 8006
            "user": "root@pam",     # or token-based auth
            "token_name": "detonator",
            "token_value": "xxxxxxxx-xxxx-...",
            "verify_ssl": false,    # optional, default false for self-signed
            "node": "pve",          # Proxmox node name
        }
    """

    def __init__(self) -> None:
        self._api: Any = None  # proxmoxer.ProxmoxAPI instance
        self._node: str = ""

    async def configure(self, config: dict) -> None:
        from proxmoxer import ProxmoxAPI

        connect_kwargs: dict[str, Any] = {
            "host": config["host"],
            "port": config.get("port", 8006),
            "verify_ssl": config.get("verify_ssl", False),
        }

        if "token_name" in config and "token_value" in config:
            connect_kwargs["user"] = config["user"]
            connect_kwargs["token_name"] = config["token_name"]
            connect_kwargs["token_value"] = config["token_value"]
        elif "password" in config:
            connect_kwargs["user"] = config["user"]
            connect_kwargs["password"] = config["password"]
        else:
            raise ValueError("Proxmox config must include token or password auth")

        self._api = await asyncio.to_thread(ProxmoxAPI, **connect_kwargs)
        self._node = config["node"]
        logger.info("Connected to Proxmox node %s at %s", self._node, config["host"])

    def _qemu(self) -> Any:
        """Shortcut to the /nodes/{node}/qemu API path."""
        return self._api.nodes(self._node).qemu

    async def list_vms(self) -> list[VMInfo]:
        raw: list[dict] = await asyncio.to_thread(self._qemu().get)
        result = []
        for vm in raw:
            vmid = str(vm["vmid"])
            snapshots = await self._list_snapshots(vmid)
            result.append(
                VMInfo(
                    vm_id=vmid,
                    name=vm.get("name", vmid),
                    state=_STATUS_MAP.get(vm.get("status", ""), VMState.UNKNOWN),
                    snapshots=snapshots,
                )
            )
        return result

    async def _list_snapshots(self, vm_id: str) -> list[str]:
        raw: list[dict] = await asyncio.to_thread(
            self._qemu()(vm_id).snapshot.get
        )
        return [s["name"] for s in raw if s["name"] != "current"]

    async def get_state(self, vm_id: str) -> VMState:
        status: dict = await asyncio.to_thread(
            self._qemu()(vm_id).status.current.get
        )
        return _STATUS_MAP.get(status.get("status", ""), VMState.UNKNOWN)

    async def revert(self, vm_id: str, snapshot_id: str) -> None:
        state = await self.get_state(vm_id)
        if state == VMState.RUNNING:
            logger.info("VM %s is running — stopping before revert", vm_id)
            await self.stop(vm_id, force=True)
            await self._wait_for_state(vm_id, VMState.STOPPED)

        logger.info("Reverting VM %s to snapshot %s", vm_id, snapshot_id)
        upid: str = await asyncio.to_thread(
            self._qemu()(vm_id).snapshot(snapshot_id).rollback.post
        )
        await self._wait_for_task(upid)

    async def start(self, vm_id: str) -> None:
        logger.info("Starting VM %s", vm_id)
        await asyncio.to_thread(self._qemu()(vm_id).status.start.post)

    async def stop(self, vm_id: str, *, force: bool = False) -> None:
        if force:
            logger.info("Force-stopping VM %s", vm_id)
            await asyncio.to_thread(self._qemu()(vm_id).status.stop.post)
        else:
            logger.info("Gracefully shutting down VM %s", vm_id)
            await asyncio.to_thread(self._qemu()(vm_id).status.shutdown.post)

    async def get_console_url(self, vm_id: str) -> str:
        ticket: dict = await asyncio.to_thread(
            self._qemu()(vm_id).spiceproxy.post
        )
        return ticket.get("proxy", "")

    async def get_network_info(self, vm_id: str) -> NetworkInfo:
        config: dict = await asyncio.to_thread(
            self._qemu()(vm_id).config.get
        )

        bridge = None
        mac = None
        for key, value in config.items():
            if key.startswith("net") and isinstance(value, str):
                parts = dict(p.split("=", 1) for p in value.split(",") if "=" in p)
                bridge = parts.get("bridge")
                # MAC is the value before the first comma in virtio=MAC,bridge=...
                for segment in value.split(","):
                    if segment.startswith("virtio="):
                        mac = segment.split("=", 1)[1]
                break

        ip = None
        try:
            interfaces: list[dict] = await asyncio.to_thread(
                self._qemu()(vm_id).agent("network-get-interfaces").get
            )
            for iface in interfaces.get("result", interfaces) if isinstance(interfaces, dict) else interfaces:
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4" and not addr["ip-address"].startswith("127."):
                        ip = addr["ip-address"]
                        break
                if ip:
                    break
        except Exception:
            logger.debug("QEMU guest agent not available for VM %s — IP unknown", vm_id)

        return NetworkInfo(ip_address=ip, mac_address=mac, bridge=bridge)

    async def _wait_for_state(
        self, vm_id: str, target: VMState, *, timeout: float = 30, poll: float = 1
    ) -> None:
        """Poll until the VM reaches the target state or timeout."""
        elapsed = 0.0
        while elapsed < timeout:
            if await self.get_state(vm_id) == target:
                return
            await asyncio.sleep(poll)
            elapsed += poll
        raise TimeoutError(
            f"VM {vm_id} did not reach state {target} within {timeout}s"
        )

    async def _wait_for_task(
        self, upid: str, *, timeout: float = 60, poll: float = 1
    ) -> None:
        """Poll a Proxmox task until it exits, then raise if it did not succeed.

        Proxmox returns a UPID (Unique Process ID) string for async operations
        such as snapshot rollbacks.  The task must reach ``status == "stopped"``
        before the caller proceeds; otherwise the VM remains locked and
        subsequent API calls (e.g. start) will fail with a lock error.
        """
        elapsed = 0.0
        while elapsed < timeout:
            result: dict = await asyncio.to_thread(
                self._api.nodes(self._node).tasks(upid).status.get
            )
            if result.get("status") == "stopped":
                exit_status = result.get("exitstatus", "")
                if exit_status != "OK":
                    raise RuntimeError(
                        f"Proxmox task {upid} failed: {exit_status}"
                    )
                return
            await asyncio.sleep(poll)
            elapsed += poll
        raise TimeoutError(f"Proxmox task {upid} did not complete within {timeout}s")
