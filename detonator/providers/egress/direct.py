"""Direct egress provider — NAT + nftables on the orchestrator's own kernel.

Routing topology:
    sandbox CIDR (ens19/vmbr1) → orchestrator → uplink (ens18) → internet

Activation installs a policy routing rule + default route in table 100 so that
sandbox packets are steered to the uplink regardless of the host default route,
then loads an nftables table named ``detonator`` that:
  - MASQUERADEs sandbox traffic out the uplink interface
  - Drops sandbox → LAN traffic (isolation invariant, when ``lan_cidr`` is set)
  - Accepts established/related return traffic
  - Drops any other sandbox forward attempts

Deactivation removes the nftables table and then flushes table 100 + removes
the policy rule — idempotent, safe to call even if activate() was never called.

Required config keys::

    {
        "uplink_interface": "ens18",        # NIC that has internet access
        "sandbox_cidr": "192.168.100.0/24", # IP range of the agent VM network
        "gateway": "192.168.0.1",           # LAN gateway reachable via uplink_interface
        "lan_cidr": "192.168.1.0/24",       # host LAN to block (optional but recommended)
    }

The orchestrator process must have CAP_NET_ADMIN (i.e. run as root) for
``nft``, ``sysctl``, and ``ip`` to succeed.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import httpx

from detonator.providers.egress._routing import add_policy_route, remove_policy_route
from detonator.providers.egress.base import EgressProvider, PreflightResult

logger = logging.getLogger(__name__)

_TABLE = "detonator"
_TABLE_ID = 100  # dedicated routing table for direct egress; adjust if conflicts with host tables
_RULE_PRIORITY = 1000
_IP_ECHO_URL = "https://api.ipify.org?format=json"


class DirectEgressProvider(EgressProvider):
    """Egress over the orchestrator's uplink NIC using nftables MASQUERADE."""

    def __init__(self) -> None:
        self._uplink: str = ""
        self._sandbox_cidr: str = ""
        self._gateway: str = ""
        self._lan_cidr: str | None = None

    # ── EgressProvider interface ─────────────────────────────────

    async def configure(self, config: dict) -> None:
        self._uplink = config["uplink_interface"]
        self._sandbox_cidr = config["sandbox_cidr"]
        self._gateway = config["gateway"]
        self._lan_cidr = config.get("lan_cidr")
        logger.info(
            "DirectEgressProvider configured: uplink=%s sandbox=%s gateway=%s lan=%s",
            self._uplink,
            self._sandbox_cidr,
            self._gateway,
            self._lan_cidr,
        )

    async def activate(self, vm_id: str) -> None:
        """Enable IP forwarding, install policy route, and load the nftables ruleset."""
        logger.info("vm=%s activating direct egress", vm_id)

        await self._run_cmd("sysctl", "-w", "net.ipv4.ip_forward=1")

        await add_policy_route(
            self._run_cmd,
            table_id=_TABLE_ID,
            sandbox_cidr=self._sandbox_cidr,
            uplink_iface=self._uplink,
            gateway=self._gateway,
            rule_priority=_RULE_PRIORITY,
        )

        ruleset = self._build_ruleset()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".nft", delete=False, prefix="detonator-"
        ) as f:
            f.write(ruleset)
            ruleset_path = f.name

        try:
            await self._run_cmd("nft", "-f", ruleset_path)
            logger.info("nftables table %r loaded", _TABLE)
        finally:
            Path(ruleset_path).unlink(missing_ok=True)

    async def deactivate(self, vm_id: str) -> None:
        """Remove the nftables table and policy route. Idempotent."""
        logger.info("vm=%s deactivating direct egress", vm_id)
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

        await remove_policy_route(
            self._run_cmd,
            table_id=_TABLE_ID,
            sandbox_cidr=self._sandbox_cidr,
            rule_priority=_RULE_PRIORITY,
        )

    async def preflight_check(self, vm_id: str) -> PreflightResult:
        """Verify the egress path by confirming the orchestrator has a public IP."""
        try:
            public_ip = await self.get_public_ip()
            logger.info("Preflight: orchestrator public IP=%s via direct egress", public_ip)
            return PreflightResult(
                passed=True,
                public_ip=public_ip,
                details=[f"Public IP via direct egress: {public_ip}"],
            )
        except Exception as exc:
            logger.warning("Preflight public-IP check failed: %s", exc)
            return PreflightResult(
                passed=False,
                details=[f"Public IP check failed: {exc}"],
            )

    async def get_public_ip(self) -> str:
        """Return the public IP address the orchestrator egresses from."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_IP_ECHO_URL)
            resp.raise_for_status()
            return resp.json()["ip"]

    # ── Internal helpers ─────────────────────────────────────────

    def _build_ruleset(self) -> str:
        """Generate the nftables ruleset string for direct egress."""
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
