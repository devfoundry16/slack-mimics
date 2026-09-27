import pytest

from slackmimic.state.store import CachedUser, StateStore


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "state.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def test_cursor_roundtrip(store):
    assert await store.get_last_ts("C1") is None
    await store.set_last_ts("C1", "1000.0001")
    assert await store.get_last_ts("C1") == "1000.0001"
    # upsert overwrites
    await store.set_last_ts("C1", "1000.0002")
    assert await store.get_last_ts("C1") == "1000.0002"


async def test_message_map_roundtrip(store):
    assert await store.get_target_ts("C1", "1.1") is None
    await store.record_mapping("C1", "1.1", "D1", "9.9")
    assert await store.get_target_ts("C1", "1.1") == ("D1", "9.9")


async def test_message_map_delete(store):
    await store.record_mapping("C1", "1.1", "D1", "9.9")
    await store.delete_mapping("C1", "1.1")
    assert await store.get_target_ts("C1", "1.1") is None


async def test_message_map_upsert(store):
    await store.record_mapping("C1", "1.1", "D1", "9.9")
    await store.record_mapping("C1", "1.1", "D1", "8.8")
    assert await store.get_target_ts("C1", "1.1") == ("D1", "8.8")


async def test_user_cache_roundtrip(store):
    assert await store.get_user("U1") is None
    await store.put_user(CachedUser("U1", "Alice", "http://x/a.png"))
    got = await store.get_user("U1")
    assert got == CachedUser("U1", "Alice", "http://x/a.png")
    # upsert updates
    await store.put_user(CachedUser("U1", "Alice B", "http://x/b.png"))
    got = await store.get_user("U1")
    assert got.name == "Alice B"
    assert got.icon_url == "http://x/b.png"


async def test_not_connected_raises(tmp_path):
    s = StateStore(str(tmp_path / "x.sqlite3"))
    with pytest.raises(RuntimeError, match="not connected"):
        await s.get_last_ts("C1")


async def test_message_map_reverse_lookup(store):
    assert await store.get_source_ts("D1", "9.9") is None
    await store.record_mapping("C1", "1.1", "D1", "9.9")
    assert await store.get_source_ts("D1", "9.9") == ("C1", "1.1")


async def test_migrates_pending_outbound_thread_ts(tmp_path):
    import sqlite3

    from slackmimic.state.store import PendingOutbound

    path = str(tmp_path / "old.sqlite3")
    # The pending_outbound table as it was before thread_ts existed.
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE pending_outbound (
                id TEXT PRIMARY KEY, target_channel TEXT NOT NULL,
                target_ts TEXT NOT NULL, source_channel TEXT NOT NULL,
                text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                card_channel TEXT NOT NULL DEFAULT '', card_ts TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO pending_outbound (id, target_channel, target_ts, source_channel,"
            " text, created_at) VALUES ('old', 'D1', '1.1', 'C1', 'hi', 0)"
        )

    async with StateStore(path) as s:
        assert (await s.get_pending("old")).thread_ts == ""
        p = PendingOutbound(
            id="new", target_channel="D1", target_ts="2.2", source_channel="C1",
            text="re", status="pending", card_channel="", card_ts="", created_at=0.0,
            thread_ts="1.1",
        )
        await s.create_pending(p)
        assert await s.get_pending("new") == p


async def test_known_users(store):
    assert not await store.has_known_users()
    await store.add_known_users(["U1", "U2", "U1"])
    assert await store.has_known_users()
    assert await store.is_known_user("U1") and await store.is_known_user("U2")
    assert not await store.is_known_user("U3")
