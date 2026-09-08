"""Send a single test message to a target-workspace channel.

    uv run python scripts/send_test.py --channel C0XXXX --text "hello from the mirror"
    uv run python scripts/send_test.py --channel C0XXXX --text "hi" --username "Alice" --icon https://…/a.png

Verifies the bot token and posting path (including author mimicking) before you
run the full daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from slackmimic.client_hs import SlackApiError
from slackmimic.sink.client_target import TargetClient


async def _main(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    token = os.environ.get("TARGET_BOT_TOKEN", "").strip()
    if not token:
        print("✗ Missing TARGET_BOT_TOKEN in the environment / .env", file=sys.stderr)
        return 1

    async with TargetClient(token) as target:
        try:
            who = await target.auth_test()
            print(f"✓ Bot authenticated: {who.get('user')} @ {who.get('url')}")
            ts = await target.post_message(
                args.channel,
                args.text,
                username=args.username,
                icon_url=args.icon,
            )
            print(f"✓ Posted to {args.channel} (ts={ts})")
        except SlackApiError as exc:
            print(f"✗ Slack error: {exc}", file=sys.stderr)
            if exc.error == "not_in_channel":
                print("  → invite the bot to the channel: /invite @your-bot", file=sys.stderr)
            elif exc.error == "channel_not_found":
                print("  → check the channel ID (must be a C… id from your workspace)", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True, help="target channel id (C…)")
    parser.add_argument("--text", default="Test message from Slack Mimic ✅")
    parser.add_argument("--username", default=None, help="override display name")
    parser.add_argument("--icon", default=None, help="override avatar URL")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
