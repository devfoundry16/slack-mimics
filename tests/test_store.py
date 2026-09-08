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
