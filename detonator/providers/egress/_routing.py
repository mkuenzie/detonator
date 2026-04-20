"""Shared policy-routing helpers for direct and tether egress providers.

Each provider uses a dedicated routing table (table_id) so sandbox traffic is
steered to the correct uplink instead of following the host's default route:

  - direct  → table 100
  - tether  → table 101

Operators with custom routing tables in that range must adjust the constants
in the respective provider modules.

Policy-rule priority is 1000 (well below main's 32766).  Both providers use
the same priority because they must never be active simultaneously.
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


async def add_policy_route(
    run_cmd: RunCmd,
    *,
    table_id: int,
    sandbox_cidr: str,
    uplink_iface: str,
    gateway: str,
    rule_priority: int = 1000,
) -> None:
    """Install a default route + policy rule so sandbox traffic uses uplink."""
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

    rc, _, stderr = await run_cmd(
        "ip", "rule", "add",
        "from", sandbox_cidr,
        "lookup", str(table_id),
        "priority", str(rule_priority),
        check=False,
    )
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning(
            "ip rule add (table %d) rc=%d: %s", table_id, rc, stderr.strip()
        )


async def remove_policy_route(
    run_cmd: RunCmd,
    *,
    table_id: int,
    sandbox_cidr: str,
    rule_priority: int = 1000,
) -> None:
    """Remove policy rule and flush provider routing table. Idempotent."""
    rc, _, stderr = await run_cmd(
        "ip", "rule", "del",
        "from", sandbox_cidr,
        "lookup", str(table_id),
        "priority", str(rule_priority),
        check=False,
    )
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning(
            "ip rule del (table %d) rc=%d: %s", table_id, rc, stderr.strip()
        )

    rc, _, stderr = await run_cmd(
        "ip", "route", "flush", "table", str(table_id),
        check=False,
    )
    if rc != 0 and not _is_idempotent_error(stderr):
        logger.warning(
            "ip route flush table %d rc=%d: %s", table_id, rc, stderr.strip()
        )
