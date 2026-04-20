"""Tests for DirectEgressProvider.

All nft / sysctl / ip calls are intercepted by patching ``_run_cmd`` on the
provider instance.  No subprocess is ever spawned.  The public-IP check
is intercepted via pytest-httpx.
"""

from __future__ import annotations

import pytest

from detonator.providers.egress.direct import DirectEgressProvider, _TABLE, _TABLE_ID

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def provider():
    p = DirectEgressProvider()
    await p.configure(
        {
            "uplink_interface": "ens18",
            "sandbox_cidr": "192.168.100.0/24",
            "gateway": "192.168.0.1",
            "lan_cidr": "192.168.1.0/24",
        }
    )
    return p


@pytest.fixture
async def provider_no_lan():
    """Provider without a lan_cidr — isolation rule should be omitted."""
    p = DirectEgressProvider()
    await p.configure(
        {
            "uplink_interface": "ens18",
            "sandbox_cidr": "192.168.100.0/24",
            "gateway": "192.168.0.1",
        }
    )
    return p


# ── configure ────────────────────────────────────────────────────


async def test_configure_stores_fields(provider):
    assert provider._uplink == "ens18"
    assert provider._sandbox_cidr == "192.168.100.0/24"
    assert provider._gateway == "192.168.0.1"
    assert provider._lan_cidr == "192.168.1.0/24"


async def test_configure_lan_cidr_optional(provider_no_lan):
    assert provider_no_lan._lan_cidr is None


async def test_configure_requires_gateway():
    """configure() must raise KeyError when gateway is absent."""
    p = DirectEgressProvider()
    with pytest.raises(KeyError):
        await p.configure(
            {
                "uplink_interface": "ens18",
                "sandbox_cidr": "192.168.100.0/24",
            }
        )


# ── _build_ruleset ───────────────────────────────────────────────


async def test_ruleset_contains_masquerade(provider):
    ruleset = provider._build_ruleset()
    assert "masquerade" in ruleset
    assert "ens18" in ruleset
    assert "192.168.100.0/24" in ruleset


async def test_ruleset_contains_lan_drop_when_configured(provider):
    ruleset = provider._build_ruleset()
    assert "192.168.1.0/24" in ruleset
    assert "drop" in ruleset


async def test_ruleset_omits_lan_drop_when_not_configured(provider_no_lan):
    ruleset = provider_no_lan._build_ruleset()
    # Only the sandbox CIDR should appear; no lan_cidr drop rule.
    assert "192.168.1.0/24" not in ruleset


async def test_ruleset_has_postrouting_and_forward_chains(provider):
    ruleset = provider._build_ruleset()
    assert "postrouting" in ruleset
    assert "forward" in ruleset
    assert "ct state established,related accept" in ruleset


# ── activate ─────────────────────────────────────────────────────


async def test_activate_calls_sysctl_and_nft(provider):
    """activate() must enable IP forwarding, install policy route, and load the nftables ruleset."""
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
    await provider.activate("100")

    cmds = [" ".join(c) for c in calls]
    assert any("sysctl" in c and "ip_forward" in c for c in cmds), (
        "sysctl net.ipv4.ip_forward=1 not called"
    )
    assert any("ip" in c and "route" in c and "add" in c and "192.168.0.1" in c for c in cmds), (
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
    assert any("nft" in c and "-f" in c for c in cmds), "nft -f <ruleset> not called"


# ── deactivate ───────────────────────────────────────────────────


async def test_deactivate_deletes_table(provider):
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, check: bool = True) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
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
            return 1, "", "Error: table not found"
        return 0, "", ""

    provider._run_cmd = fake_run  # type: ignore[method-assign]
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
    await provider.deactivate("100")


# ── preflight_check ──────────────────────────────────────────────


async def test_preflight_returns_passed_with_public_ip(provider, httpx_mock):
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        json={"ip": "1.2.3.4"},
    )
    result = await provider.preflight_check("100")
    assert result.passed is True
    assert result.public_ip == "1.2.3.4"
    assert any("1.2.3.4" in d for d in result.details)


async def test_preflight_returns_failed_on_http_error(provider, httpx_mock):
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        status_code=503,
    )
    result = await provider.preflight_check("100")
    assert result.passed is False
    assert result.details


# ── get_public_ip ────────────────────────────────────────────────


async def test_get_public_ip_returns_ip(provider, httpx_mock):
    httpx_mock.add_response(
        url="https://api.ipify.org?format=json",
        json={"ip": "203.0.113.5"},
    )
    ip = await provider.get_public_ip()
    assert ip == "203.0.113.5"
