"""Relaying vanta-core messages with files to HeartStamp."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from slackmimic.client_hs import HeartStampClient
from slackmimic.config import ChannelMap, Config, Secrets
from slackmimic.reverse.relay import send_pending
from slackmimic.sink.client_target import TargetClient
from slackmimic.source.reader import SourceReader
from slackmimic.state.store import PendingOutbound, StateStore


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "s.sqlite3"))
    await s.connect()
    yield s
    await s.close()


def _pending(text="look", files=(("a.png", "https://files.slack.com/a"),), thread_ts=""):
    return PendingOutbound(
        id="p1", target_channel="C_DST", target_ts="1.1", source_channel="C_SRC",
        text=text, status="pending", card_channel="", card_ts="", created_at=0.0,
        thread_ts=thread_ts, files=files,
    )


# --- store ---------------------------------------------------------------------

async def test_pending_keeps_files(store):
    await store.create_pending(_pending())
    assert (await store.get_pending("p1")).files == (("a.png", "https://files.slack.com/a"),)


async def test_migrates_pending_outbound_files(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE pending_outbound (id TEXT PRIMARY KEY, target_channel TEXT NOT NULL,"
        " target_ts TEXT NOT NULL, source_channel TEXT NOT NULL, text TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', card_channel TEXT NOT NULL DEFAULT '',"
        " card_ts TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
        " thread_ts TEXT NOT NULL DEFAULT '')"
    )
    db.execute("INSERT INTO pending_outbound (id, target_channel, target_ts, source_channel,"
               " text, created_at) VALUES ('old', 'C_DST', '1.1', 'C_SRC', 'hi', 0)")
    db.commit()
    db.close()
    async with StateStore(path) as s:
        assert (await s.get_pending("old")).files == ()


async def test_reverse_files_roundtrip(store):
    assert await store.has_reverse_file(["F1"]) is False
    await store.record_reverse_files(["F1", "F2"])
    assert await store.has_reverse_file(["F9", "F2"]) is True


# --- anti-echo -----------------------------------------------------------------

class HistoryHS:
    def __init__(self, messages):
        self._messages = messages

    async def conversations_history(self, channel, **kw):
        return {"messages": self._messages}


async def test_reader_skips_message_carrying_relayed_file(store):
    cfg = Config(secrets=Secrets("x", "d", "b"), channels=[ChannelMap("C_SRC", "C_DST", "g")])
    msg = {"ts": "100.1", "user": "U1", "text": "look", "files": [{"id": "F_UP", "name": "a.png"}]}
    q: asyncio.Queue = asyncio.Queue()
    await store.set_last_ts("C_SRC", "50.0")
    await store.record_reverse_files(["F_UP"])
    assert await SourceReader(HistoryHS([msg]), cfg, store, q).poll_once() == 0


# --- send_pending --------------------------------------------------------------

class FakeTarget:
    def __init__(self, broken=()):
        self.broken = set(broken)

    async def download_file(self, url):
        if url in self.broken:
            raise RuntimeError("missing_scope")
        return b"bytes:" + url.encode()


class FakeHS:
    def __init__(self, found_ts="200.2"):
        self.calls = []
        self._found_ts = found_ts
        self._n = 0

    async def chat_post_message(self, channel, text, *, thread_ts=None):
        self.calls.append(("post", channel, text, thread_ts))
        return "300.3"

    async def reserve_upload(self, filename, content):
        self._n += 1
        self.calls.append(("reserve", filename, content))
        return f"F{self._n}"

    async def share_uploads(self, channel, uploads, *, text, thread_ts=None):
        self.calls.append(("share", channel, list(uploads), text, thread_ts))

    async def find_file_message(self, channel, file_id, *, thread_ts=None):
        self.calls.append(("find", channel, file_id, thread_ts))
        return self._found_ts


async def test_send_text_only(store):
    hs = FakeHS()
    result = await send_pending(_pending(files=()), "hi", store=store, hs=hs, target=FakeTarget(),
                                hs_thread_ts=None)
    assert hs.calls == [("post", "C_SRC", "hi", None)]
    assert result.hs_ts == "300.3" and result.failed == []
    assert await store.is_reverse_post("C_SRC", "300.3")
    assert await store.get_target_ts("C_SRC", "300.3") == ("C_DST", "1.1")


async def test_send_files_with_text_as_one_message(store):
    hs = FakeHS()
    p = _pending(files=(("a.png", "https://files.slack.com/a"), ("b.pdf", "https://files.slack.com/b")))
    result = await send_pending(p, "look", store=store, hs=hs, target=FakeTarget(), hs_thread_ts="9.9")
    assert [c[0] for c in hs.calls] == ["reserve", "reserve", "share", "find"]
    assert hs.calls[0] == ("reserve", "a.png", b"bytes:https://files.slack.com/a")
    assert hs.calls[2] == ("share", "C_SRC", [("F1", "a.png"), ("F2", "b.pdf")], "look", "9.9")
    # Recorded before sharing, so the forward mirror never echoes it back.
    assert await store.has_reverse_file(["F1"]) and await store.has_reverse_file(["F2"])
    assert result.hs_ts == "200.2"
    assert await store.is_reverse_post("C_SRC", "200.2")


async def test_send_reports_files_that_could_not_be_downloaded(store):
    hs = FakeHS()
    p = _pending(files=(("a.png", "https://files.slack.com/a"), ("b.pdf", "https://files.slack.com/b")))
    target = FakeTarget(broken={"https://files.slack.com/b"})
    result = await send_pending(p, "look", store=store, hs=hs, target=target, hs_thread_ts=None)
    assert result.failed == ["b.pdf"]
    assert hs.calls[1] == ("share", "C_SRC", [("F1", "a.png")], "look", None)


async def test_send_falls_back_to_text_when_no_file_downloads(store):
    hs = FakeHS()
    result = await send_pending(_pending(), "look", store=store, hs=hs,
                                target=FakeTarget(broken={"https://files.slack.com/a"}), hs_thread_ts=None)
    assert hs.calls == [("post", "C_SRC", "look", None)]
    assert result.failed == ["a.png"]


async def test_send_with_nothing_left_raises(store):
    with pytest.raises(ValueError):
        await send_pending(_pending(text=""), "", store=store, hs=FakeHS(),
                           target=FakeTarget(broken={"https://files.slack.com/a"}), hs_thread_ts=None)


async def test_send_without_found_ts_still_succeeds(store):
    result = await send_pending(_pending(), "look", store=store, hs=FakeHS(found_ts=None),
                                target=FakeTarget(), hs_thread_ts=None)
    assert result.hs_ts is None and result.failed == []


# --- HTTP clients ----------------------------------------------------------------

def _recording(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url="https://slack.com/api")


async def test_hs_reserve_upload_and_share():
    def handler(request):
        if request.url.path.endswith("files.getUploadURLExternal"):
            return httpx.Response(200, json={"ok": True, "upload_url": "https://files.slack.com/upload/v1/x", "file_id": "F7"})
        if request.url.host == "files.slack.com":
            return httpx.Response(200, text="OK")
        return httpx.Response(200, json={"ok": True, "files": [{"id": "F7"}]})

    seen, http = _recording(handler)
    hs = HeartStampClient("xoxc", "xoxd", client=http)
    assert await hs.reserve_upload("a.png", b"abc") == "F7"
    assert seen[1].url == "https://files.slack.com/upload/v1/x" and seen[1].content == b"abc"
    await hs.share_uploads("C_SRC", [("F7", "a.png")], text="look", thread_ts="9.9")
    form = dict(httpx.QueryParams(seen[2].content.decode()))
    assert json.loads(form["files"]) == [{"id": "F7", "title": "a.png"}]
    assert form["channel_id"] == "C_SRC" and form["initial_comment"] == "look" and form["thread_ts"] == "9.9"


async def test_hs_reserve_upload_refuses_non_slack_upload_url():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "upload_url": "https://evil.example/u", "file_id": "F7"})

    seen, http = _recording(handler)
    with pytest.raises(ValueError):
        await HeartStampClient("xoxc", "xoxd", client=http).reserve_upload("a.png", b"abc")
    assert len(seen) == 1


async def test_hs_find_file_message():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "messages": [
            {"ts": "5.5", "text": "other"},
            {"ts": "6.6", "files": [{"id": "F7"}]},
        ]})

    _, http = _recording(handler)
    hs = HeartStampClient("xoxc", "xoxd", client=http)
    assert await hs.find_file_message("C_SRC", "F7", delay=0) == "6.6"
    assert await hs.find_file_message("C_SRC", "F_NONE", attempts=2, delay=0) is None


async def test_target_download_rejects_login_page():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>sign in</html>")

    _, http = _recording(handler)
    with pytest.raises(Exception, match="files:read"):
        await TargetClient("xoxb", client=http).download_file("https://files.slack.com/f/a.png")


async def test_target_download_only_from_slack():
    seen, http = _recording(lambda r: httpx.Response(200, content=b"x"))
    target = TargetClient("xoxb", client=http)
    with pytest.raises(ValueError):
        await target.download_file("https://evil.example/a.png")
    assert seen == []
    assert await target.download_file("https://files.slack.com/f/a.png") == b"x"
