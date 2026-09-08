"""Create matching channels in the target workspace and write config.yaml.

    uv run python scripts/provision.py                 # all channels, exact names
    uv run python scripts/provision.py --public-only
    uv run python scripts/provision.py --prefix hs- --out config.yaml
    uv run python scripts/provision.py --dry-run       # list plan, create nothing

Reuses same-named target channels instead of failing. The bot must have
channels:manage, groups:write, channels:read, groups:read, channels:join.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from slackmimic.client_hs import HeartStampClient
from slackmimic.provision import (
    list_source_channels,
    normalize_channel_name,
    provision,
    render_config_yaml,
)
from slackmimic.sink.client_target import TargetClient


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(args.env)

    hs_token = os.environ.get("HS_XOXC_TOKEN", "").strip()
    hs_cookie = os.environ.get("HS_D_COOKIE", "").strip()
    bot_token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not (hs_token and hs_cookie and bot_token):
        print("✗ Missing HS_XOXC_TOKEN, HS_D_COOKIE, or TARGET_BOT_TOKEN", file=sys.stderr)
        return 1

    include_private = not args.public_only

    async with (
        HeartStampClient(hs_token, hs_cookie) as hs,
        TargetClient(bot_token) as target,
    ):
        if args.dry_run:
            sources = await list_source_channels(hs, include_private=include_private)
            print(f"Would provision {len(sources)} channel(s):")
            for s in sources:
                tag = "private" if s.is_private else "public"
                print(f"  #{s.name}  ->  #{normalize_channel_name(args.prefix + s.name)}  ({tag})")
            return 0

        result = await provision(
            hs, target, include_private=include_private, prefix=args.prefix
        )

    Path(args.out).write_text(
        render_config_yaml(result.mappings), encoding="utf-8"
    )

    print("\n=== Provisioning summary ===")
    print(f"  created:      {len(result.created)}")
    print(f"  reused:       {len(result.reused)}")
    print(f"  mapped total: {len(result.mappings)}")
    if result.needs_invite:
        print(f"  needs /invite (private, bot not a member): {', '.join(result.needs_invite)}")
    if result.failed:
        print("  failed:")
        for name, err in result.failed:
            print(f"    #{name}: {err}")
    print(f"\nWrote {args.out} with {len(result.mappings)} channel mapping(s).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-only", action="store_true", help="skip private channels")
    parser.add_argument("--prefix", default="", help="prefix for target channel names")
    parser.add_argument("--out", default="config.yaml", help="config file to write")
    parser.add_argument("--dry-run", action="store_true", help="print plan; create nothing")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
