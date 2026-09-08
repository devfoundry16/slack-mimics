"""Verify both sets of credentials before running the daemon.

    uv run python scripts/check_auth.py [--env .env]

Prints the resolved HeartStamp identity and target bot identity, or a clear
error telling you which credential is bad.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from slackmimic.client_hs import AuthError, HeartStampClient
from slackmimic.config import ConfigError, load_secrets
from slackmimic.sink.client_target import TargetClient


async def _main(env_file: str | None) -> int:
    try:
        secrets = load_secrets(env_file)
    except ConfigError as exc:
        print(f"✗ config: {exc}", file=sys.stderr)
        return 1

    ok = True

    async with HeartStampClient(secrets.hs_xoxc_token, secrets.hs_d_cookie) as hs:
        try:
            info = await hs.auth_test()
            print(f"✓ HeartStamp: {info.get('user')} @ {info.get('url')}")
        except AuthError as exc:
            print(f"✗ HeartStamp: {exc}", file=sys.stderr)
            ok = False
        except Exception as exc:
            print(f"✗ HeartStamp: {exc}", file=sys.stderr)
            ok = False

    async with TargetClient(secrets.target_bot_token) as target:
        try:
            info = await target.auth_test()
            print(f"✓ Target bot: {info.get('user')} @ {info.get('url')}")
        except Exception as exc:
            print(f"✗ Target bot: {exc}", file=sys.stderr)
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.env)))
