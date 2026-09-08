import asyncio
import time

import pytest

from slackmimic.config import ChannelMap, Config, Secrets
from slackmimic.source.reader import SourceReader
from slackmimic.state.store import StateStore


def make_config(**kw):
    return Config(
        secrets=Secrets("x", "d", "b"),
        channels=[ChannelMap("C_SRC", "C_DST", "general")],
        **kw,
    )


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "s.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def test_initialize_cursors_from_now(store):
    reader = SourceReader(None, make_config(), store, asyncio.Queue())
    before = time.time()
    await reader.initialize_cursors()
    ts = float(await store.get_last_ts("C_SRC"))
    # Cursor is ~now, not in the past.
    assert ts >= before - 1


async def test_initialize_cursors_backfill(store):
    reader = SourceReader(None, make_config(), store, asyncio.Queue())
    await reader.initialize_cursors(backfill_days=7)
    ts = float(await store.get_last_ts("C_SRC"))
    seven_days = 7 * 86400
    # Cursor is ~7 days in the past.
    assert abs((time.time() - ts) - seven_days) < 5


async def test_initialize_cursors_does_not_overwrite(store):
    reader = SourceReader(None, make_config(), store, asyncio.Queue())
    await store.set_last_ts("C_SRC", "12345.6789")
    await reader.initialize_cursors(backfill_days=7)
    # Existing cursor preserved — restarts never re-backfill.
    assert await store.get_last_ts("C_SRC") == "12345.6789"
