"""Seed given source channels' cursors to *now* so they skip the 7-day backfill.

    uv run python scripts/set_cursor_now.py D0AAA D0BBB ...

Run while the daemon is stopped. Channels seeded here already have a cursor, so
initialize_cursors() leaves them untouched (start from now, no history).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

from slackmimic.state.store import StateStore


async def _main(source_ids: list[str]) -> int:
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
    db_path = str(cfg.get("db_path", "slackmimic.sqlite3"))
    now = f"{time.time():.6f}"

    async with StateStore(db_path) as store:
        for cid in source_ids:
            await store.set_last_ts(cid, now)
            print(f"✓ {cid} cursor set to now ({now})")
    return 0


if __name__ == "__main__":
    ids = sys.argv[1:]
    if not ids:
        print("usage: set_cursor_now.py <source_id> [source_id ...]", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(asyncio.run(_main(ids)))
