"""CLI tool to activate/deactivate/preflight a named egress provider.

Usage::

    sudo detonator-egress activate  direct  --config config.toml
    sudo detonator-egress preflight direct  --config config.toml
    sudo detonator-egress deactivate direct --config config.toml

The provider name must match a key under ``[egress.*]`` in the config file.
Requires CAP_NET_ADMIN (run as root or via sudo).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from detonator.config import load_config
from detonator.providers.egress.base import EgressProvider

logger = logging.getLogger(__name__)

_VM_ID = "ctl"  # synthetic VM id used when running outside a real run


async def _build_provider(name: str, config_path: Path) -> EgressProvider:
    cfg = load_config(config_path)
    egress_cfg = cfg.egress.get(name)
    if egress_cfg is None:
        available = list(cfg.egress.keys()) or ["(none configured)"]
        print(
            f"error: no egress config named {name!r}\n"
            f"available: {', '.join(available)}",
            file=sys.stderr,
        )
        sys.exit(1)

    ptype = egress_cfg.type.lower()
    if ptype == "direct":
        from detonator.providers.egress.direct import DirectEgressProvider

        provider: EgressProvider = DirectEgressProvider()
    elif ptype == "tether":
        from detonator.providers.egress.tether import TetherEgressProvider

        provider = TetherEgressProvider()
    else:
        print(f"error: unknown provider type {ptype!r}", file=sys.stderr)
        sys.exit(1)

    await provider.configure(egress_cfg.settings)
    return provider


async def _run(action: str, name: str, config_path: Path) -> int:
    provider = await _build_provider(name, config_path)

    if action == "activate":
        await provider.activate(_VM_ID)
        print(f"[{name}] activated")

    elif action == "deactivate":
        await provider.deactivate(_VM_ID)
        print(f"[{name}] deactivated")

    elif action == "preflight":
        result = await provider.preflight_check(_VM_ID)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{name}] preflight {status}")
        if result.public_ip:
            print(f"  public_ip : {result.public_ip}")
        for line in result.details:
            print(f"  {line}")
        return 0 if result.passed else 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="detonator-egress",
        description="Activate, deactivate, or preflight-check a detonator egress provider.",
    )
    parser.add_argument(
        "action",
        choices=["activate", "deactivate", "preflight"],
        help="Operation to perform",
    )
    parser.add_argument(
        "provider",
        help="Egress provider name as declared in config (e.g. 'direct', 'tether')",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        metavar="PATH",
        help="Path to config.toml (default: config.toml)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    rc = asyncio.run(_run(args.action, args.provider, config_path))
    sys.exit(rc)


if __name__ == "__main__":
    main()
