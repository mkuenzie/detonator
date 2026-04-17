"""USB tether egress provider — NAT + nftables routing via iPhone/Android RNDIS interface.

Routing topology:
    sandbox CIDR (ens19/vmbr1) → orchestrator → uplink (enxXXXXXXXXXXXX) → carrier NAT → internet

Activation installs an nftables table named ``detonator-tether`` that:
  - MASQUERADEs sandbox traffic out the tether interface
  - Drops sandbox → LAN traffic (isolation invariant, when ``lan_cidr`` is set)
  - Accepts established/related return traffic
  - Drops any other sandbox forward attempts

Deactivation deletes the table — idempotent, safe to call even if activate()
was never called or the table was already removed.

Required config keys::

    {
        "uplink_interface": "enxea98eebb97c7", # USB tether NIC (ipheth/RNDIS)
        "sandbox_cidr": "192.168.100.0/24",    # IP range of the agent VM network
        "lan_cidr": "192.168.1.0/24",          # host LAN to block (optional but recommended)
    }

The orchestrator process must have CAP_NET_ADMIN (i.e. run as root) for
``nft`` and ``sysctl`` to succeed.

Assumptions:
  - The tether interface has an IPv4 lease before activate() is called.
    On iPhone, this means Personal Hotspot is enabled and the host-side
    systemd-networkd unit (see docs/tether-setup.md) has run DHCP.
  - ``usbmuxd`` is running and the phone has been paired (Trust This Computer
    accepted) — this is a one-time step per host.

The provider does not bring the interface up or trigger DHCP — that is the
operator's responsibility.  preflight_check() will fail fast with a clear
message if the interface has no IPv4 lease.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import httpx

from detonator.providers.egress.base import EgressProvider, PreflightResult

logger = logging.getLogger(__name__)

_TABLE = "detonator-tether"
_IP_ECHO_URL = "https://api.ipify.org?format=json"


class TetherEgressProvider(EgressProvider):
    """Egress over a USB-tethered phone using nftables MASQUERADE."""

    def __init__(self) -> None:
        self._uplink: str = ""
        self._sandbox_cidr: str = ""
        self._lan_cidr: str | None = None

    # ── EgressProvider interface ─────────────────────────────────

    async def configure(self, config: dict) -> None:
        self._uplink = config["uplink_interface"]
        self._sandbox_cidr = config["sandbox_cidr"]
        self._lan_cidr = config.get("lan_cidr")
        logger.info(
            "TetherEgressProvider configured: uplink=%s sandbox=%s lan=%s",
            self._uplink,
            self._sandbox_cidr,
            self._lan_cidr,
        )

    async def activate(self, vm_id: str) -> None:
        """Enable IP forwarding and load the nftables ruleset."""
        logger.info("vm=%s activating tether egress", vm_id)

        await self._run_cmd("sysctl", "-w", "net.ipv4.ip_forward=1")

        ruleset = self._build_ruleset()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".nft", delete=False, prefix="detonator-tether-"
        ) as f:
            f.write(ruleset)
            ruleset_path = f.name

        try:
            await self._run_cmd("nft", "-f", ruleset_path)
            logger.info("nftables table %r loaded", _TABLE)
        finally:
            Path(ruleset_path).unlink(missing_ok=True)

    async def deactivate(self, vm_id: str) -> None:
        """Remove the nftables table. Idempotent — safe to call when not active."""
        logger.info("vm=%s deactivating tether egress", vm_id)
        rc, _, stderr = await self._run_cmd(
            "nft", "delete", "table", "ip", _TABLE, check=False
        )
        if rc != 0:
            msg = stderr.strip().lower()
            if "no such file" in msg or "table not found" in msg or "no table found" in msg:
                logger.debug("nftables table %r already absent", _TABLE)
            else:
                logger.warning(
                    "nft delete table returned unexpected rc=%d: %s", rc, stderr.strip()
                )

    async def preflight_check(self, vm_id: str) -> PreflightResult:
        """Verify the tether uplink has an IPv4 lease, then confirm public IP."""
        uplink_ip = self._get_uplink_ipv4()
        if uplink_ip is None:
            msg = (
                f"tether uplink {self._uplink!r} has no IPv4 lease"
                " — is Personal Hotspot active?"
            )
            logger.warning("Preflight failed: %s", msg)
            return PreflightResult(passed=False, details=[msg])

        try:
            public_ip = await self.get_public_ip()
            logger.info(
                "Preflight: orchestrator public IP=%s via tether egress (uplink=%s)",
                public_ip,
                uplink_ip,
            )
            return PreflightResult(
                passed=True,
                public_ip=public_ip,
                details=[f"Public IP via tether egress: {public_ip}"],
            )
        except Exception as exc:
            logger.warning("Preflight public-IP check failed: %s", exc)
            return PreflightResult(
                passed=False,
                details=[f"Public IP check failed: {exc}"],
            )

    async def get_public_ip(self) -> str:
        """Return the public IP as seen through the tether interface."""
        uplink_ip = self._get_uplink_ipv4()
        transport = httpx.AsyncHTTPTransport(local_address=uplink_ip)
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
            resp = await client.get(_IP_ECHO_URL)
            resp.raise_for_status()
            return resp.json()["ip"]

    # ── Internal helpers ─────────────────────────────────────────

    def _get_uplink_ipv4(self) -> str | None:
        """Return the first IPv4 address on the uplink interface, or None."""
        try:
            import subprocess  # noqa: PLC0415

            out = subprocess.check_output(
                ["ip", "-4", "-br", "addr", "show", self._uplink],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            # output: "enxXXX  UP  172.20.10.2/28 "
            for token in out.split():
                if "/" in token:
                    return token.split("/")[0]
        except Exception:
            pass

        return None

    def _build_ruleset(self) -> str:
        """Generate the nftables ruleset string for tether egress."""
        lines = [
            f"table ip {_TABLE} {{",
            "    chain postrouting {",
            "        type nat hook postrouting priority srcnat; policy accept;",
            f'        ip saddr {self._sandbox_cidr} oif "{self._uplink}" masquerade',
            "    }",
            "",
            "    chain forward {",
            "        type filter hook forward priority filter; policy accept;",
            "        ct state established,related accept",
        ]
        if self._lan_cidr:
            lines.append(
                f"        ip saddr {self._sandbox_cidr} ip daddr {self._lan_cidr} drop"
            )
        lines += [
            f'        ip saddr {self._sandbox_cidr} oif "{self._uplink}" accept',
            f"        ip saddr {self._sandbox_cidr} drop",
            "    }",
            "}",
        ]
        return "\n".join(lines) + "\n"

    async def _run_cmd(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        """Execute a subprocess command and return (returncode, stdout, stderr).

        Raises ``RuntimeError`` on non-zero exit when ``check=True``.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else 0
        stdout, stderr = stdout_b.decode(), stderr_b.decode()
        if check and rc != 0:
            raise RuntimeError(
                f"Command {list(args)!r} failed (rc={rc}): {stderr.strip()}"
            )
        return rc, stdout, stderr
