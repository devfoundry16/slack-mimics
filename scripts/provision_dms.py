"""Add DM + group-DM mirroring: create private target channels, merge into config.

    uv run python scripts/provision_dms.py                 # exclude the DM with "keith"
    uv run python scripts/provision_dms.py --exclude keith --exclude bob
    uv run python scripts/provision_dms.py --dry-run       # list plan, create nothing

1:1 DMs whose counterpart's name matches an --exclude term are skipped; group
DMs are always included. Existing config.yaml channels and settings are kept;
only new DM/group-DM mappings are appended.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from slackmimic.client_hs import HeartStampClient
from slackmimic.provision import (
    list_source_dms,
    list_target_index,
    normalize_channel_name,
    provision_sources,
    render_config_yaml,
)
from slackmimic.sink.client_target import TargetClient


def _load_config(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(args.env)

    hs_token = os.environ.get("HS_XOXC_TOKEN", "").strip()
    hs_cookie = os.environ.get("HS_D_COOKIE", "").strip()
    bot_token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not (hs_token and hs_cookie and bot_token):
        print("✗ Missing credentials in .env", file=sys.stderr)
        return 1

    cfg = _load_config(args.config)
    existing_channels = cfg.get("channels") or []
    existing_source_ids = {str(c["source"]) for c in existing_channels}

    async with (
        HeartStampClient(hs_token, hs_cookie) as hs,
        TargetClient(bot_token) as target,
    ):
        dms = await list_source_dms(hs, exclude_user_names=args.exclude)
        # Skip any DM already mapped in config.
        new_dms = [d for d in dms if d.id not in existing_source_ids]

        if args.dry_run:
            print(f"Would add {len(new_dms)} DM/group-DM channel(s) (excluding: {args.exclude}):")
            for d in new_dms:
                print(f"  {d.id}  ->  #{normalize_channel_name(d.name)}  (private)")
            return 0

        target_index = await list_target_index(target)
        result = await provision_sources(target, new_dms, existing=target_index)

    # Merge: keep existing channels + settings, append new DM mappings.
    merged = [
        (str(c["source"]), str(c["target"]), str(c.get("label", "")))
        for c in existing_channels
    ] + result.mappings

    out = render_config_yaml(
        merged,
        poll_interval_seconds=float(cfg.get("poll_interval_seconds", 5.0)),
        use_websocket=bool(cfg.get("use_websocket", True)),
        db_path=str(cfg.get("db_path", "slackmimic.sqlite3")),
        backfill_days=float(cfg.get("backfill_days", 0.0)),
    )
    Path(args.config).write_text(out, encoding="utf-8")

    print("\n=== DM provisioning summary ===")
    print(f"  created:      {len(result.created)}")
    print(f"  reused:       {len(result.reused)}")
    print(f"  new mappings: {len(result.mappings)}")
    if result.failed:
        for name, err in result.failed:
            print(f"    #{name}: {err}")
    print(f"\nUpdated {args.config}: {len(merged)} total channel mapping(s).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exclude", action="append", default=["keith"],
        help="name term whose 1:1 DM to skip (repeatable; default: keith)",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
