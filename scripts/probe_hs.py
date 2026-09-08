"""Read-only probe of the HeartStamp workspace.

    uv run python scripts/probe_hs.py                # list your channels
    uv run python scripts/probe_hs.py --channel C123 # + last messages in C123

Use it to discover channel ids for config.yaml and to confirm the reader sees
real messages. Reads only; never writes to HeartStamp.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from slackmimic.client_hs import HeartStampClient
from slackmimic.source import normalize


async def _main(channel: str | None, limit: int, env_file: str) -> int:
    load_dotenv(env_file)
    token = os.environ.get("HS_XOXC_TOKEN", "").strip()
    cookie = os.environ.get("HS_D_COOKIE", "").strip()
    if not token or not cookie:
        print("✗ Missing HS_XOXC_TOKEN or HS_D_COOKIE", file=sys.stderr)
        return 1

    async with HeartStampClient(token, cookie) as hs:
        print("Channels you're a member of:")
        cursor = None
        while True:
            body = await hs.users_conversations(cursor=cursor)
            for ch in body.get("channels", []):
                kind = "private" if ch.get("is_private") else "public"
                print(f"  {ch.get('id')}  #{ch.get('name')}  ({kind})")
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        if channel:
            print(f"\nLast {limit} message(s) in {channel}:")
            body = await hs.conversations_history(channel, limit=limit)
            msgs = sorted(body.get("messages", []), key=lambda m: float(m.get("ts", 0)))
            for m in msgs:
                evt = normalize.message_to_event(channel, m)
                if evt is None:
                    continue
                files = f" [{len(evt.files)} file(s)]" if evt.files else ""
                thread = " (reply)" if evt.thread_ts else ""
                text = (evt.text or "").replace("\n", " ")[:80]
                print(f"  {evt.ts}  {evt.user}: {text}{files}{thread}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=None, help="channel id to sample")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.channel, args.limit, args.env)))
