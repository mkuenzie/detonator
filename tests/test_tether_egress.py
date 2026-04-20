"""Tests for TetherEgressProvider.

All nft / sysctl / ip calls are intercepted by patching ``_run_cmd`` on the
provider instance.  No subprocess is ever spawned.  The public-IP check
is intercepted via pytest-httpx.  The IPv4 uplink-liveness check is
intercepted by patching ``_get_uplink_ipv4``.
"""

from __future__ import annotations

import pytest

from detonator.providers.egress.tether import TetherEgressProvider, _TABLE, _TABLE_ID

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def provider():
    p = TetherEgressProvider()
    await p.configure(
        {
            "uplink_interface": "enxea98eebb97c7",
            "sandbox_cidr": "192.168.100.0/24",
            "gateway": "172.20.10.1",
            "lan_cidr": "192.168.1.0/24",
        }
    )
    return p


@pytest.fixture
async def provider_no_lan():
    """Provider without a lan_cidr — isolation rule should be omitted."""
    p = TetherEgressProvider()
    await p.configure(
        {
            "uplink_interface": "enxea98eebb97c7",
            "sandbox_cidr": "192.168.100.0/24",
            "gateway": "172.20.10.1",
        }
    )
    return p


# ── configure ────────────────────────────────────────────────────


async def test_configure_stores_fields(provider):
    assert provider._uplink == "enxea98eebb97c7"
    assert provider._sandbox_cidr == "192.168.100.0/24"
    assert provider._gateway == "172.20.10.1"
    assert provider._lan_cidr == "192.168.1.0/24"


async def test_configure_lan_cidr_optional(provider_no_lan):
    assert provider_no_lan._lan_cidr is None


async def test_configure_requires_gateway():
    """configure() must raise KeyError when gateway is absent."""
    p = TetherEgressProvider()
    with pytest.raises(KeyError):
        await p.configure(
            {
                "uplink_interface": "enxea98eebb97c7",
                "sandbox_cidr": "192.168.100.0/24",
            }
        )


# ── _build_ruleset ───────────────────────────────────────────────


async def test_ruleset_contains_masquerade(provider):
    ruleset = provider._build_ruleset()
    assert "masquerade" in ruleset
    assert "enxea98eebb97c7" in ruleset
    assert "192.168.100.0/24" in ruleset


async def test_ruleset_contains_lan_drop_when_configured(provider):
    ruleset = provider._build_ruleset()
    assert "192.168.1.0/24" in ruleset
    assert "drop" in ruleset


async def test_ruleset_omits_lan_drop_when_not_configured(provider_no_lan):
    ruleset = provider_no_lan._build_ruleset()
    assert "192.168.1.0/24" not in ruleset


async def test_ruleset_has_postrouting_and_forward_chains(provider):
    ruleset = provider._build_ruleset()
    assert "postrouting" in ruleset
    assert "forward" in ruleset
    assert "ct state established,related accept" in ruleset


async def test_ruleset_uses_tether_table_name(provider):
    ruleset = provider._build_ruleset()
    assert _TABLE in ruleset
    assert _TABLE == "detonator-tether"


# ── activate ─────────────────────────────────────────────────────


async def test_activate_calls_sysctl_and_nft(provider):
    """activate() must enable IP forwarding, install policy routes, and load the nftables ruleset."""
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    provider._get_uplink_cidr = lambda: "172.20.10.2/28"  # type: ignore[method-assign]
    await provider.activate("100")

    cmds = [" ".join(c) for c in calls]
    assert any("sysctl" in c and "ip_forward" in c for c in cmds), (
        "sysctl net.ipv4.ip_forward=1 not called"
    )
    assert any("ip" in c and "route" in c and "add" in c and "172.20.10.1" in c for c in cmds), (
        "ip route add default via gateway not called"
    )
    assert any(
        "ip" in c and "rule" in c and "add" in c and "192.168.100.0/24" in c and "main" in c
        for c in cmds
    ), "ip rule add bypass (sandbox→sandbox to main) not called"
    assert any(
        "ip" in c and "rule" in c and "add" in c and "192.168.100.0/24" in c and "main" not in c
        for c in cmds
    ), "ip rule add forwarding (sandbox→uplink) not called"
    assert any(
        "ip" in c and "rule" in c and "add" in c and "172.20.10.2/28" in c for c in cmds
    ), "ip rule add from uplink cidr not called"
    assert any("nft" in c and "-f" in c for c in cmds), "nft -f <ruleset> not called"


async def test_activate_skips_uplink_rule_when_no_lease(provider):
    """activate() must not raise when the uplink has no lease yet; logs a warning instead."""
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    provider._get_uplink_cidr = lambda: None  # type: ignore[method-assign]
    await provider.activate("100")  # must not raise

    cmds = [" ".join(c) for c in calls]
    # Sandbox rule must still be installed.
    assert any(
        "ip" in c and "rule" in c and "add" in c and "192.168.100.0/24" in c for c in cmds
    ), "sandbox policy rule not installed even without uplink lease"


# ── deactivate ───────────────────────────────────────────────────


async def test_deactivate_deletes_table(provider):
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    provider._get_uplink_cidr = lambda: "172.20.10.2/28"  # type: ignore[method-assign]
    await provider.deactivate("100")

    cmds = [" ".join(a) for a in calls]
    assert any(
        "nft" in c and "delete" in c and _TABLE in c for c in cmds
    ), "nft delete table not called"
    assert any(
        "ip" in c and "rule" in c and "del" in c and "192.168.100.0/24" in c for c in cmds
    ), "ip rule del not called"
    assert any(
        "ip" in c and "rule" in c and "del" in c and "192.168.100.0/24" in c and "main" in c
        for c in cmds
    ), "ip rule del bypass (sandbox→sandbox main) not called"
    assert any(
        "ip" in c and "route" in c and "flush" in c and str(_TABLE_ID) in c for c in cmds
    ), "ip route flush table not called"


async def test_deactivate_is_idempotent_when_table_absent(provider):
    """deactivate() must not raise when the nftables table does not exist."""

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        if "delete" in args:
            return 1, "", "Error: No such file or directory"
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    provider._get_uplink_cidr = lambda: "172.20.10.2/28"  # type: ignore[method-assign]
    await provider.deactivate("100")


async def test_deactivate_is_idempotent_when_route_absent(provider):
    """deactivate() must not raise when route/rule are already absent."""

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        if "rule" in args and "del" in args:
            return 2, "", "RTNETLINK answers: No such process"
        if "flush" in args:
            return 2, "", "RTNETLINK answers: No such file or directory"
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    provider._get_uplink_cidr = lambda: "172.20.10.2/28"  # type: ignore[method-assign]
    await provider.deactivate("100")


# ── preflight_check ──────────────────────────────────────────────


async def test_preflight_returns_passed_with_public_ip(provider, httpx_mock):
    provider._get_uplink_ipv4 = lambda: "172.20.10.2"  # type: ignore[method-assign]
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        json={"ip": "203.0.113.10"},
    )
    result = await provider.preflight_check("100")
    assert result.passed is True
    assert result.public_ip == "203.0.113.10"
    assert any("203.0.113.10" in d for d in result.details)


async def test_preflight_returns_failed_on_http_error(provider, httpx_mock):
    provider._get_uplink_ipv4 = lambda: "172.20.10.2"  # type: ignore[method-assign]
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        status_code=503,
    )
    result = await provider.preflight_check("100")
    assert result.passed is False
    assert result.details


async def test_preflight_fails_when_uplink_has_no_ipv4(provider):
    """preflight_check must fail fast when the tether interface has no IPv4 lease."""
    provider._get_uplink_ipv4 = lambda: None  # type: ignore[method-assign]
    result = await provider.preflight_check("100")
    assert result.passed is False
    assert any("no IPv4 lease" in d for d in result.details)
    assert any("enxea98eebb97c7" in d for d in result.details)


# ── get_public_ip ────────────────────────────────────────────────


async def test_get_public_ip_returns_ip(provider, httpx_mock):
    provider._get_uplink_ipv4 = lambda: "172.20.10.2"  # type: ignore[method-assign]
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        json={"ip": "203.0.113.5"},
    )
    ip = await provider.get_public_ip()
    assert ip == "203.0.113.5"
