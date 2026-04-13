"""Tests for ProxmoxProvider with mocked Proxmox API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from detonator.models import VMState
from detonator.providers.vm.proxmox import ProxmoxProvider


@pytest.fixture
def provider():
    p = ProxmoxProvider()
    p._api = MagicMock()
    p._node = "pve"
    return p


async def _run(coro_or_func, *args, **kwargs):
    """Stand-in for asyncio.to_thread that just calls the function directly."""
    if callable(coro_or_func):
        return coro_or_func(*args, **kwargs)
    return coro_or_func


def _patch_to_thread():
    return patch(
        "detonator.providers.vm.proxmox.asyncio.to_thread",
        side_effect=_run,
    )


async def test_list_vms(provider: ProxmoxProvider):
    qemu = provider._api.nodes(provider._node).qemu
    qemu.get.return_value = [
        {"vmid": 100, "name": "sandbox", "status": "stopped"},
    ]
    qemu.return_value.snapshot.get.return_value = [
        {"name": "clean"},
        {"name": "current"},
    ]

    with _patch_to_thread():
        vms = await provider.list_vms()

    assert len(vms) == 1
    assert vms[0].vm_id == "100"
    assert vms[0].name == "sandbox"
    assert vms[0].state == VMState.STOPPED
    assert "clean" in vms[0].snapshots
    assert "current" not in vms[0].snapshots


async def test_get_state_running(provider: ProxmoxProvider):
    provider._api.nodes(provider._node).qemu.return_value.status.current.get.return_value = {
        "status": "running"
    }
    with _patch_to_thread():
        state = await provider.get_state("100")
    assert state == VMState.RUNNING


async def test_get_state_unknown(provider: ProxmoxProvider):
    provider._api.nodes(provider._node).qemu.return_value.status.current.get.return_value = {
        "status": "migrating"
    }
    with _patch_to_thread():
        state = await provider.get_state("100")
    assert state == VMState.UNKNOWN


async def test_start(provider: ProxmoxProvider):
    with _patch_to_thread():
        await provider.start("100")
    provider._api.nodes(provider._node).qemu.return_value.status.start.post.assert_called_once()


async def test_stop_graceful(provider: ProxmoxProvider):
    with _patch_to_thread():
        await provider.stop("100")
    provider._api.nodes(provider._node).qemu.return_value.status.shutdown.post.assert_called_once()


async def test_stop_force(provider: ProxmoxProvider):
    with _patch_to_thread():
        await provider.stop("100", force=True)
    provider._api.nodes(provider._node).qemu.return_value.status.stop.post.assert_called_once()


async def test_revert_stopped_vm(provider: ProxmoxProvider):
    provider._api.nodes(provider._node).qemu.return_value.status.current.get.return_value = {
        "status": "stopped"
    }
    with _patch_to_thread():
        await provider.revert("100", "clean")
    provider._api.nodes(provider._node).qemu.return_value.snapshot.return_value.rollback.post.assert_called_once()


async def test_get_network_info_with_agent(provider: ProxmoxProvider):
    qemu_vm = provider._api.nodes(provider._node).qemu.return_value
    qemu_vm.config.get.return_value = {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1",
    }
    qemu_vm.agent.return_value.get.return_value = {
        "result": [
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address": "10.0.0.5", "ip-address-type": "ipv4"},
                ],
            }
        ]
    }

    with _patch_to_thread():
        info = await provider.get_network_info("100")

    assert info.mac_address == "AA:BB:CC:DD:EE:FF"
    assert info.bridge == "vmbr1"
    assert info.ip_address == "10.0.0.5"


async def test_get_network_info_no_agent(provider: ProxmoxProvider):
    qemu_vm = provider._api.nodes(provider._node).qemu.return_value
    qemu_vm.config.get.return_value = {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1",
    }
    qemu_vm.agent.return_value.get.side_effect = Exception("QEMU guest agent not available")

    with _patch_to_thread():
        info = await provider.get_network_info("100")

    assert info.mac_address == "AA:BB:CC:DD:EE:FF"
    assert info.bridge == "vmbr1"
    assert info.ip_address is None
