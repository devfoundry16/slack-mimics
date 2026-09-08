"""Remove group-DM mirroring: archive those channels and drop them from config.

    uv run python scripts/remove_group_dms.py            # archive + rewrite config
    uv run python scripts/remove_group_dms.py --dry-run  # show plan, change nothing

Group-DM entries are identified by a config label starting with "mpdm-". 1:1 DMs
(label "dm-…") and regular channels are kept. Archiving is reversible; the
channels can be unarchived in Slack.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from slackmimic.client_hs import SlackApiError
from slackmimic.provision import render_config_yaml
from slackmimic.sink.client_target import TargetClient

_BENIGN = {"already_archived", "channel_not_found", "method_not_supported_for_channel_type"}


def _is_group_dm(label: str) -> bool:
    return label.startswith("mpdm-")


async def _main(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not token:
        print("✗ Missing TARGET_BOT_TOKEN", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    channels = cfg.get("channels") or []

    group = [c for c in channels if _is_group_dm(str(c.get("label", "")))]
    keep = [c for c in channels if not _is_group_dm(str(c.get("label", "")))]
    # Unique target ids to archive (some group DMs mapped to the same target).
    to_archive = list(dict.fromkeys(str(c["target"]) for c in group))

    print(f"Group DMs to remove: {len(group)} entries -> {len(to_archive)} channel(s)")
    print(f"Keeping: {len(keep)} channel(s) (regular + 1:1 DMs)")

    if args.dry_run:
        print("\n(dry-run) no changes made.")
        return 0

    archived = failed = 0
    async with TargetClient(token) as target:
        for cid in to_archive:
            try:
                await target.conversations_archive(cid)
                archived += 1
                await asyncio.sleep(0.4)
            except SlackApiError as exc:
                if exc.error in _BENIGN:
                    archived += 1
                else:
                    print(f"✗ archive {cid}: {exc.error}", file=sys.stderr)
                    failed += 1

    merged = [
        (str(c["source"]), str(c["target"]), str(c.get("label", "")))
        for c in keep
    ]
    out = render_config_yaml(
        merged,
        poll_interval_seconds=float(cfg.get("poll_interval_seconds", 5.0)),
        use_websocket=bool(cfg.get("use_websocket", True)),
        db_path=str(cfg.get("db_path", "slackmimic.sqlite3")),
        backfill_days=float(cfg.get("backfill_days", 0.0)),
    )
    Path(args.config).write_text(out, encoding="utf-8")

    print(f"\nDone: archived {archived}, failed {failed}.")
    print(f"Updated {args.config}: {len(merged)} channel mapping(s) remain.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
