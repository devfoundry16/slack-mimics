"""Verify only the HeartStamp session credentials (token + d cookie).

    uv run python scripts/check_hs_auth.py [--env .env]

Prints the resolved HeartStamp identity, or a clear error. Does not require the
target bot token.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from slackmimic.client_hs import AuthError, HeartStampClient


async def _main(env_file: str | None) -> int:
    load_dotenv(env_file)
    token = os.environ.get("HS_XOXC_TOKEN", "").strip()
    cookie = os.environ.get("HS_D_COOKIE", "").strip()
    if not token or not cookie:
        print("✗ Missing HS_XOXC_TOKEN or HS_D_COOKIE", file=sys.stderr)
        return 1

    async with HeartStampClient(token, cookie) as hs:
        try:
            info = await hs.auth_test()
        except AuthError as exc:
            print(f"✗ HeartStamp auth failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"✗ HeartStamp error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    print("✓ HeartStamp authenticated")
    print(f"    user:  {info.get('user')}  ({info.get('user_id')})")
    print(f"    team:  {info.get('team')}  ({info.get('team_id')})")
    print(f"    url:   {info.get('url')}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.env)))
