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


# --- threads -----------------------------------------------------------------

class ThreadHS:
    """Fake HS that honours ``oldest`` like Slack does.

    ``history`` holds top-level messages only; ``replies`` maps a parent ts to
    its replies (the parent itself is always returned first, as Slack does).
    """

    def __init__(self, history, replies=None):
        self._history = history
        self._replies = replies or {}
        self.parent_fetches = []
        self.thread_fetches = []

    async def conversations_history(self, channel, *, oldest=None, limit=100, cursor=None):
        return {"messages": [m for m in self._history if _after(m["ts"], oldest)]}

    async def conversations_replies(self, channel, ts, *, oldest=None, limit=100, cursor=None):
        parent = next(m for m in self._history if m["ts"] == ts)
        if limit == 1:
            self.parent_fetches.append(ts)
            return {"messages": [parent]}
        self.thread_fetches.append(ts)
        replies = [m for m in self._replies.get(ts, []) if _after(m["ts"], oldest)]
        return {"messages": [parent] + replies}


def _after(ts, oldest):
    return oldest is None or float(ts) > float(oldest)


def _ago(seconds):
    return f"{time.time() - seconds:.6f}"


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _old_thread():
    """A 3-day-old parent with a reply from a minute ago; cursor is 1 day old."""
    parent_ts, reply_ts, cursor = _ago(3 * 86400), _ago(60), _ago(86400)
    parent = {"ts": parent_ts, "user": "U1", "text": "parent", "thread_ts": parent_ts,
              "reply_count": 1, "latest_reply": reply_ts}
    reply = {"ts": reply_ts, "user": "U2", "text": "reply", "thread_ts": parent_ts}
    return ThreadHS([parent], {parent_ts: [reply]}), parent_ts, reply_ts, cursor


async def test_plain_poll_cannot_see_reply_to_old_thread(store):
    hs, _, _, cursor = _old_thread()
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)
    await SourceReader(hs, make_config(), store, q).poll_once()
    assert q.empty()


async def test_rescan_finds_reply_to_old_thread(store):
    hs, parent_ts, reply_ts, cursor = _old_thread()
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)
    await store.record_mapping("C_SRC", parent_ts, "C_DST", "900.1")  # parent mirrored

    await SourceReader(hs, make_config(), store, q).poll_once(rescan_threads=True)

    # Only the reply: the already-mirrored parent isn't re-emitted.
    assert [e.ts for e in _drain(q)] == [reply_ts]
    assert hs.parent_fetches == []
    assert await store.get_last_ts("C_SRC") == reply_ts


async def test_unmirrored_parent_is_queued_before_reply(store):
    hs, parent_ts, reply_ts, cursor = _old_thread()
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)

    await SourceReader(hs, make_config(), store, q).poll_once(rescan_threads=True)

    events = _drain(q)
    assert [e.ts for e in events] == [parent_ts, reply_ts]
    assert events[1].thread_ts == parent_ts
    assert hs.parent_fetches == [parent_ts]


async def test_new_parent_is_not_fetched_again(store):
    parent_ts, reply_ts = _ago(120), _ago(60)
    parent = {"ts": parent_ts, "user": "U1", "text": "parent", "thread_ts": parent_ts,
              "reply_count": 1, "latest_reply": reply_ts}
    reply = {"ts": reply_ts, "user": "U2", "text": "reply", "thread_ts": parent_ts}
    hs = ThreadHS([parent], {parent_ts: [reply]})
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", _ago(3600))

    await SourceReader(hs, make_config(), store, q).poll_once()

    assert [e.ts for e in _drain(q)] == [parent_ts, reply_ts]
    assert hs.parent_fetches == []


async def test_reverse_post_parent_is_not_fetched(store):
    hs, parent_ts, reply_ts, cursor = _old_thread()
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)
    await store.record_reverse_post("C_SRC", parent_ts)

    await SourceReader(hs, make_config(), store, q).poll_once(rescan_threads=True)

    assert [e.ts for e in _drain(q)] == [reply_ts]
    assert hs.parent_fetches == []


async def test_rescan_respects_lookback(store):
    hs, _, _, cursor = _old_thread()  # parent is 3 days old
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)
    reader = SourceReader(hs, make_config(thread_lookback_days=2), store, q)
    await reader.poll_once(rescan_threads=True)
    assert q.empty()


async def test_reply_not_lost_when_channel_cursor_moves_past_it(store):
    """A plain poll can't see a reply to an existing thread, but it does move the
    channel cursor past it when a newer top-level message arrives. The rescan
    must still find the reply."""
    parent_ts, reply_ts, newer_ts = _ago(600), _ago(300), _ago(100)
    parent = {"ts": parent_ts, "user": "U1", "text": "parent", "thread_ts": parent_ts,
              "reply_count": 1, "latest_reply": reply_ts}
    reply = {"ts": reply_ts, "user": "U2", "text": "reply", "thread_ts": parent_ts}
    newer = {"ts": newer_ts, "user": "U3", "text": "new top-level"}
    hs = ThreadHS([parent, newer], {parent_ts: [reply]})
    q: asyncio.Queue = asyncio.Queue()
    # State after the parent and the newer message were mirrored by plain polls.
    await store.record_mapping("C_SRC", parent_ts, "C_DST", "900.1")
    await store.record_mapping("C_SRC", newer_ts, "C_DST", "900.2")
    await store.set_last_ts("C_SRC", newer_ts)

    await SourceReader(hs, make_config(), store, q).poll_once(rescan_threads=True)

    assert [e.ts for e in _drain(q)] == [reply_ts]


async def test_synced_thread_is_not_refetched(store):
    hs, parent_ts, reply_ts, cursor = _old_thread()
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", cursor)
    await store.record_mapping("C_SRC", parent_ts, "C_DST", "900.1")
    reader = SourceReader(hs, make_config(), store, q)

    await reader.poll_once(rescan_threads=True)
    assert [e.ts for e in _drain(q)] == [reply_ts]
    # Nothing new in the thread: the next rescan doesn't fetch it again.
    await reader.poll_once(rescan_threads=True)
    assert q.empty()
    assert hs.thread_fetches == [parent_ts]


# --- websocket ---------------------------------------------------------------

class WsHS:
    """Fake HS client pointing the reader at a local websocket server."""

    def __init__(self, url):
        self._url = url
        self.ws_headers = {"Cookie": "d=secret"}

    async def rtm_connect(self):
        return self._url


async def _serve(handler):
    from websockets.asyncio.server import serve

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


async def test_websocket_sends_session_cookie(store):
    """Without the d cookie Slack sends an error event and drops the socket."""
    import websockets

    seen = {}

    async def handler(ws):
        seen["cookie"] = ws.request.headers.get("Cookie")
        await ws.send('{"type": "hello"}')
        await ws.send('{"type": "message", "channel": "C_SRC", "ts": "5.5", "user": "U1", "text": "hi"}')
        await ws.close()

    server, url = await _serve(handler)
    q: asyncio.Queue = asyncio.Queue()
    try:
        # A socket that closes right after connecting is a failure: let the
        # caller fall back to polling rather than reconnecting in a tight loop.
        with pytest.raises(websockets.ConnectionClosed):
            await SourceReader(WsHS(url), make_config(), store, q).run_websocket()
    finally:
        server.close()
    assert seen["cookie"] == "d=secret"
    assert [e.ts for e in _drain(q)] == ["5.5"]


async def test_websocket_drop_after_healthy_session_returns(store, monkeypatch):
    """A drop after a healthy session returns, so the caller catches up and reconnects."""
    from slackmimic.source import reader as reader_mod

    monkeypatch.setattr(reader_mod, "_MIN_HEALTHY_SECONDS", 0)

    async def handler(ws):
        await ws.send('{"type": "hello"}')
        await ws.close()

    server, url = await _serve(handler)
    try:
        await SourceReader(WsHS(url), make_config(), store, asyncio.Queue()).run_websocket()
    finally:
        server.close()


async def test_websocket_passes_team_join_to_handler(store):
    joined = []

    async def on_member_joined(user):
        joined.append(user["id"])

    async def handler(ws):
        await ws.send('{"type": "hello"}')
        await ws.send('{"type": "team_join", "user": {"id": "U_NEW"}}')
        await ws.close()

    server, url = await _serve(handler)
    reader = SourceReader(
        WsHS(url), make_config(), store, asyncio.Queue(), on_member_joined=on_member_joined
    )
    try:
        with pytest.raises(Exception):
            await reader.run_websocket()
        await asyncio.sleep(0)  # let the background handler run
    finally:
        server.close()
    assert joined == ["U_NEW"]


async def test_websocket_mirrors_channel_added_while_running(store):
    """A channel added to the config mid-session is mirrored without reconnecting."""
    cfg = make_config()

    async def handler(ws):
        cfg.add_channel(ChannelMap("D_NEW", "C_NEW", "dm-new"))
        await ws.send('{"type": "message", "channel": "D_NEW", "ts": "7.7", "user": "U1", "text": "hi"}')
        await ws.close()

    server, url = await _serve(handler)
    q: asyncio.Queue = asyncio.Queue()
    try:
        with pytest.raises(Exception):
            await SourceReader(WsHS(url), cfg, store, q).run_websocket()
    finally:
        server.close()
    assert [e.ts for e in _drain(q)] == ["7.7"]
