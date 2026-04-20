"""Shared policy-routing helpers for direct and tether egress providers.

Each provider uses a dedicated routing table (table_id) so sandbox traffic is
steered to the correct uplink instead of following the host's default route:

  - direct  → table 100
  - tether  → table 101

Operators with custom routing tables in that range must adjust the constants
in the respective provider modules.

Policy-rule priority is 1000 (well below main's 32766).  Both providers use
the same priority because they must never be active simultaneously.

Two rules are installed per activation (example with sandbox_cidr=192.168.100.0/24,
rule_priority=1000, table_id=101):

  priority 999:  from 192.168.100.0/24 to 192.168.100.0/24  lookup main
  priority 1000: from 192.168.100.0/24                       lookup 101

Rule 999 (the bypass) ensures the orchestrator's own traffic to the agent VM
stays on the sandbox bridge.  Without it, the rule-1000 match would steer those
packets into table 101, where they'd exit via the uplink instead of the bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# stderr fragments that indicate an idempotent no-op rather than a real failure.
_IDEMPOTENT_FRAGMENTS = frozenset(
    [
        "no such process",
        "no such file",
        "file exists",
        "rtnetlink answers: file exists",
        "cannot find device",
        "object not found",
    ]
)

RunCmd = Callable[..., Awaitable[tuple[int, str, str]]]


def _is_idempotent_error(stderr: str) -> bool:
    msg = stderr.strip().lower()
    return any(frag in msg for frag in _IDEMPOTENT_FRAGMENTS)


async def _rule(run_cmd: RunCmd, action: str, *, from_cidr: str, to_cidr: str | None,
                table: str, priority: int) -> None:
    args = ["ip", "rule", action, "from", from_cidr]
    if to_cidr:
        args += ["to", to_cidr]
    args += ["lookup", table, "priority", str(priority)]
    rc, _, stderr = await run_cmd(*args, check=False)
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning("ip rule %s rc=%d: %s", action, rc, stderr.strip())


async def add_policy_route(
    run_cmd: RunCmd,
    *,
    table_id: int,
    sandbox_cidr: str,
    uplink_iface: str,
    gateway: str,
    rule_priority: int = 1000,
) -> None:
    """Install default route + two policy rules so sandbox→internet uses uplink.

    The bypass rule (priority-1) keeps sandbox→sandbox traffic on the main table
    so the orchestrator can still reach the agent after egress is activated.
    """
    rc, _, stderr = await run_cmd(
        "ip", "route", "add", "default",
        "via", gateway, "dev", uplink_iface,
        "table", str(table_id),
        check=False,
    )
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning(
            "ip route add default (table %d) rc=%d: %s", table_id, rc, stderr.strip()
        )

    # Bypass rule: sandbox→sandbox stays in main (orchestrator ↔ agent traffic).
    await _rule(run_cmd, "add",
                from_cidr=sandbox_cidr, to_cidr=sandbox_cidr,
                table="main", priority=rule_priority - 1)

    # Forwarding rule: all other sandbox traffic goes through the provider table.
    await _rule(run_cmd, "add",
                from_cidr=sandbox_cidr, to_cidr=None,
                table=str(table_id), priority=rule_priority)


async def remove_policy_route(
    run_cmd: RunCmd,
    *,
    table_id: int,
    sandbox_cidr: str,
    rule_priority: int = 1000,
) -> None:
    """Remove both policy rules and flush provider routing table. Idempotent."""
    await _rule(run_cmd, "del",
                from_cidr=sandbox_cidr, to_cidr=None,
                table=str(table_id), priority=rule_priority)

    await _rule(run_cmd, "del",
                from_cidr=sandbox_cidr, to_cidr=sandbox_cidr,
                table="main", priority=rule_priority - 1)

    rc, _, stderr = await run_cmd(
        "ip", "route", "flush", "table", str(table_id),
        check=False,
    )
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning(
            "ip route flush table %d rc=%d: %s", table_id, rc, stderr.strip()
        )
