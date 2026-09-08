"""Invite a user to every target channel listed in config.yaml.

    uv run python scripts/invite_me.py --user U0XXXXXXX
    uv run python scripts/invite_me.py --email you@example.com   # needs users:read.email

The bot must be a member of each channel (it is, if it created them). Already-a-
member channels are skipped quietly.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import yaml
from dotenv import load_dotenv

from slackmimic.client_hs import SlackApiError
from slackmimic.sink.client_target import TargetClient

# Errors that just mean "nothing to do" for a given channel.
_BENIGN = {"already_in_channel", "cant_invite_self"}


def _target_channels(config_path: str) -> list[tuple[str, str]]:
    data = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    return [(c["target"], c.get("label", c["target"])) for c in data.get("channels", [])]


async def _main(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not token:
        print("✗ Missing TARGET_BOT_TOKEN", file=sys.stderr)
        return 1

    channels = _target_channels(args.config)
    if not channels:
        print(f"✗ No channels found in {args.config}", file=sys.stderr)
        return 1

    invited = skipped = failed = 0
    async with TargetClient(token) as target:
        user_id = args.user
        if not user_id and args.email:
            try:
                res = await target.lookup_by_email(args.email)
                user_id = res["user"]["id"]
                print(f"Resolved {args.email} -> {user_id}")
            except SlackApiError as exc:
                print(f"✗ email lookup failed: {exc}", file=sys.stderr)
                return 1
        if not user_id:
            print("✗ Provide --user U… or --email …", file=sys.stderr)
            return 1

        for channel_id, label in channels:
            try:
                await target.conversations_invite(channel_id, user_id)
                print(f"✓ invited to #{label}")
                invited += 1
            except SlackApiError as exc:
                if exc.error in _BENIGN:
                    skipped += 1
                else:
                    print(f"✗ #{label}: {exc.error}", file=sys.stderr)
                    failed += 1

    print(f"\nDone: {invited} invited, {skipped} already in, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=None, help="target member id (U…)")
    parser.add_argument("--email", default=None, help="look up member by email")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
