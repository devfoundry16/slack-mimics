"""Shorten auto-generated group-DM channel names (mpdm-… -> gdm-…).

    uv run python scripts/rename_dms.py            # rename all mpdm-* channels
    uv run python scripts/rename_dms.py --dry-run  # show the plan, rename nothing

Iterates the target workspace's channels and renames any still named like a raw
group DM. Channel ids don't change, so config.yaml keeps working untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from slackmimic.client_hs import SlackApiError
from slackmimic.provision import short_group_name
from slackmimic.sink.client_target import TargetClient


async def _list_all(target: TargetClient) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        body = await target.conversations_list(cursor=cursor)
        out.extend(body.get("channels", []))
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


def _unique(name: str, taken: set[str], channel_id: str) -> str:
    if name not in taken:
        return name
    for extra in channel_id[::-1].lower():
        candidate = f"{name}{extra}"[:80]
        if candidate not in taken:
            return candidate
    return f"{name}x"[:80]


async def _main(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not token:
        print("✗ Missing TARGET_BOT_TOKEN", file=sys.stderr)
        return 1

    async with TargetClient(token) as target:
        channels = await _list_all(target)
        taken = {c["name"] for c in channels}
        targets = [c for c in channels if str(c.get("name", "")).startswith("mpdm-")]

        renamed = failed = 0
        for ch in targets:
            old = ch["name"]
            new = short_group_name(old, str(ch["id"]))
            new = _unique(new, taken, str(ch["id"]))
            if new == old:
                continue
            if args.dry_run:
                print(f"  {old}  ->  {new}")
                taken.add(new)
                continue
            try:
                await target.conversations_rename(str(ch["id"]), new)
                taken.discard(old)
                taken.add(new)
                print(f"✓ {old}  ->  {new}")
                renamed += 1
                await asyncio.sleep(0.5)
            except SlackApiError as exc:
                print(f"✗ {old}: {exc.error}", file=sys.stderr)
                failed += 1

    if args.dry_run:
        print(f"\n{len(targets)} channel(s) would be renamed.")
    else:
        print(f"\nDone: {renamed} renamed, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
