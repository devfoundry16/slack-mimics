"""Mirror one HeartStamp conversation (e.g. a group DM or a channel).

    uv run python scripts/mirror_channel.py C0XXXXXXX --user U0AAA --user U0BBB
    uv run python scripts/mirror_channel.py C0XXXXXXX --email a@x.com,b@y.com
    uv run python scripts/mirror_channel.py C0XXXXXXX --name gdm-launch --dry-run

Creates (or reuses) a target channel (private for DMs / group DMs and private
source channels, public otherwise), adds the mapping to config.yaml
and invites the given members plus ``owner_member_id`` from config (skip with
--no-owner). Re-running for an already-mapped id just re-sends the invites.
Restart the mirror afterwards so it picks up the new mapping.
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

from slackmimic.client_hs import HeartStampClient, SlackApiError
from slackmimic.provision import (
    describe_source_channel,
    list_target_index,
    normalize_channel_name,
    provision_sources,
    render_config_yaml,
)
from slackmimic.sink.client_target import TargetClient

# Errors that just mean "nothing to do" for a given invite.
_BENIGN = {"already_in_channel", "cant_invite_self"}


def _split(values: list[str]) -> list[str]:
    """Flatten repeatable, comma-separated CLI values."""
    return [v.strip() for raw in values for v in raw.split(",") if v.strip()]


def _load_config(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


async def _resolve_members(
    target: TargetClient, user_ids: list[str], emails: list[str]
) -> tuple[list[str], int]:
    """Return (member ids, lookup failures), resolving emails to ids."""
    ids = list(user_ids)
    failed = 0
    for email in emails:
        try:
            res = await target.lookup_by_email(email)
            ids.append(str(res["user"]["id"]))
            print(f"Resolved {email} -> {ids[-1]}")
        except SlackApiError as exc:
            print(f"✗ email lookup {email}: {exc.error}", file=sys.stderr)
            failed += 1
    return list(dict.fromkeys(ids)), failed


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
    owner = str(cfg.get("owner_member_id") or "")
    user_ids = _split(args.user)
    if owner and not args.no_owner:
        user_ids.append(owner)
    emails = _split(args.email)

    mapped = next((c for c in existing_channels if str(c["source"]) == args.channel), None)

    async with (
        HeartStampClient(hs_token, hs_cookie) as hs,
        TargetClient(bot_token) as target,
    ):
        if mapped:
            target_id = str(mapped["target"])
            label = str(mapped.get("label") or target_id)
            print(f"{args.channel} is already mirrored to #{label} ({target_id}); inviting only.")
        else:
            try:
                src = await describe_source_channel(hs, args.channel)
            except SlackApiError as exc:
                print(f"✗ HeartStamp lookup {args.channel}: {exc.error}", file=sys.stderr)
                return 1
            if args.name:
                src.name = args.name
            src.name = normalize_channel_name(src.name)

            if args.dry_run:
                print(f"Would create {'private' if src.is_private else 'public'} #{src.name} for {src.id}")
                print(f"Would invite: {', '.join(user_ids + emails) or '(nobody)'}")
                return 0

            result = await provision_sources(
                target, [src], existing=await list_target_index(target), throttle_seconds=0
            )
            if result.failed:
                print(f"✗ #{src.name}: {result.failed[0][1]}", file=sys.stderr)
                return 1
            if result.needs_invite:
                print(
                    f"✗ #{src.name} already exists as a private channel the bot isn't in; "
                    "/invite the bot there or pass --name to pick another name.",
                    file=sys.stderr,
                )
                return 1
            _, target_id, label = result.mappings[0]

            merged = [
                (str(c["source"]), str(c["target"]), str(c.get("label", "")))
                for c in existing_channels
            ] + result.mappings
            Path(args.config).write_text(
                render_config_yaml(
                    merged,
                    poll_interval_seconds=float(cfg.get("poll_interval_seconds", 5.0)),
                    use_websocket=bool(cfg.get("use_websocket", True)),
                    db_path=str(cfg.get("db_path", "slackmimic.sqlite3")),
                    backfill_days=float(cfg.get("backfill_days", 0.0)),
                    reverse_enabled=bool(cfg.get("reverse_enabled", False)),
                    owner_member_id=owner,
                ),
                encoding="utf-8",
            )
            status = "reused" if result.reused else "created"
            print(f"✓ {status} #{label} ({target_id}); added to {args.config}")

        if args.dry_run:
            print(f"Would invite: {', '.join(user_ids + emails) or '(nobody)'}")
            return 0

        members, failed = await _resolve_members(target, user_ids, emails)
        invited = skipped = 0
        for uid in members:
            try:
                await target.conversations_invite(target_id, uid)
                print(f"✓ invited {uid}")
                invited += 1
            except SlackApiError as exc:
                if exc.error in _BENIGN:
                    skipped += 1
                else:
                    print(f"✗ invite {uid}: {exc.error}", file=sys.stderr)
                    failed += 1

    print(f"\nDone: {invited} invited, {skipped} already in, {failed} failed.")
    if not mapped:
        print("Restart the mirror so it starts copying this channel.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("channel", help="HeartStamp channel / group-DM id (C…, G… or D…)")
    parser.add_argument("--user", action="append", default=[], help="target member id(s) U… (repeatable or comma-separated)")
    parser.add_argument("--email", action="append", default=[], help="member email(s) to look up (needs users:read.email)")
    parser.add_argument("--name", default=None, help="target channel name (default: derived from source)")
    parser.add_argument("--no-owner", action="store_true", help="don't auto-invite owner_member_id")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
