import pytest

from slackmimic.models import EventKind, SourceFile, TargetAction
from slackmimic.sink.poster import Poster
from slackmimic.state.store import StateStore


class FakeTarget:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.deleted = []
        self.reactions_added = []
        self.reactions_removed = []
        self.uploads = []
        self._ts = 1000

    async def post_message(self, channel, text, *, username=None, icon_url=None, thread_ts=None):
        self._ts += 1
        ts = f"{self._ts}.0"
        self.posted.append(
            {"channel": channel, "text": text, "username": username, "icon_url": icon_url, "thread_ts": thread_ts, "ts": ts}
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.updated.append({"channel": channel, "ts": ts, "text": text})

    async def delete_message(self, channel, ts):
        self.deleted.append({"channel": channel, "ts": ts})

    async def add_reaction(self, channel, ts, name):
        self.reactions_added.append({"channel": channel, "ts": ts, "name": name})

    async def remove_reaction(self, channel, ts, name):
        self.reactions_removed.append({"channel": channel, "ts": ts, "name": name})

    async def upload_file(self, channel, filename, content, *, thread_ts=None, title=None):
        self.uploads.append({"channel": channel, "filename": filename, "content": content, "thread_ts": thread_ts})


class FakeHS:
    def __init__(self, data=b"filebytes", fail=False):
        self._data = data
        self._fail = fail

    async def download_file(self, url):
        if self._fail:
            raise RuntimeError("download failed")
        return self._data


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "s.sqlite3"))
    await s.connect()
    yield s
    await s.close()


def action(kind, ts, *, text="hi", thread=None, files=None, reaction=None):
    return TargetAction(
        kind=kind,
        channel="C_DST",
        source_channel="C_SRC",
        source_ts=ts,
        text=text,
        username="Alice",
        icon_url="http://x/a.png",
        source_thread_ts=thread,
        files=files or [],
        reaction=reaction,
    )


async def test_create_posts_and_maps(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1"))
    assert len(target.posted) == 1
    assert target.posted[0]["username"] == "Alice"
    assert await store.get_target_ts("C_SRC", "1.1") == ("C_DST", target.posted[0]["ts"])


async def test_create_is_idempotent(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1"))
    await poster.apply(action(EventKind.CREATE, "1.1"))
    assert len(target.posted) == 1


async def test_reply_threads_to_parent(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1"))
    parent_ts = target.posted[0]["ts"]
    await poster.apply(action(EventKind.CREATE, "2.2", thread="1.1"))
    assert target.posted[1]["thread_ts"] == parent_ts


async def test_edit_updates_mapped_message(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1", text="orig"))
    ts = target.posted[0]["ts"]
    await poster.apply(action(EventKind.EDIT, "1.1", text="edited"))
    assert target.updated == [{"channel": "C_DST", "ts": ts, "text": "edited"}]


async def test_edit_without_original_creates(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.EDIT, "1.1", text="edited"))
    assert len(target.posted) == 1
    assert not target.updated


async def test_delete_removes_and_unmaps(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1"))
    ts = target.posted[0]["ts"]
    await poster.apply(action(EventKind.DELETE, "1.1"))
    assert target.deleted == [{"channel": "C_DST", "ts": ts}]
    assert await store.get_target_ts("C_SRC", "1.1") is None


async def test_reaction_add(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS())
    await poster.apply(action(EventKind.CREATE, "1.1"))
    ts = target.posted[0]["ts"]
    await poster.apply(action(EventKind.REACTION_ADD, "1.1", reaction="tada"))
    assert target.reactions_added == [{"channel": "C_DST", "ts": ts, "name": "tada"}]


async def test_file_uploaded(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS(data=b"PNGDATA"))
    f = SourceFile(id="F1", name="a.png", mimetype="image/png", url_private="http://x/a")
    await poster.apply(action(EventKind.CREATE, "1.1", files=[f]))
    assert len(target.uploads) == 1
    assert target.uploads[0]["content"] == b"PNGDATA"


async def test_file_failure_degrades_gracefully(store):
    target = FakeTarget()
    poster = Poster(target, store, FakeHS(fail=True))
    f = SourceFile(id="F1", name="a.png", mimetype="image/png", url_private="http://x/a")
    await poster.apply(action(EventKind.CREATE, "1.1", files=[f]))
    # main message + a fallback note, no crash
    assert len(target.posted) == 2
    assert "not mirrored" in target.posted[1]["text"]
