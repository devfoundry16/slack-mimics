import pytest

from slackmimic.config import ChannelMap, Config, Secrets
from slackmimic.models import EventKind, SourceEvent, SourceFile
from slackmimic.state.store import CachedUser
from slackmimic.transform.transformer import Transformer, is_actionable


class FakeResolver:
    def __init__(self, users):
        self._users = users

    async def resolve(self, user_id):
        return self._users.get(
            user_id, CachedUser(user_id, user_id, "")
        )


def make_config():
    return Config(
        secrets=Secrets("x", "d", "b"),
        channels=[ChannelMap("C_SRC", "C_DST", "general")],
    )


@pytest.fixture
def transformer():
    users = {
        "U1": CachedUser("U1", "Alice", "http://x/alice.png"),
        "U2": CachedUser("U2", "Bob", "http://x/bob.png"),
    }
    return Transformer(make_config(), FakeResolver(users))


async def test_maps_channel_and_author(transformer):
    evt = SourceEvent(kind=EventKind.CREATE, channel="C_SRC", ts="1.1", user="U1", text="hi")
    action = await transformer.transform(evt)
    assert action is not None
    assert action.channel == "C_DST"
    assert action.username == "Alice"
    assert action.icon_url == "http://x/alice.png"
    assert action.source_channel == "C_SRC"
    assert action.source_ts == "1.1"


async def test_unmapped_channel_dropped(transformer):
    evt = SourceEvent(kind=EventKind.CREATE, channel="C_OTHER", ts="1.1", user="U1", text="hi")
    assert await transformer.transform(evt) is None


async def test_rewrites_user_mention(transformer):
    evt = SourceEvent(
        kind=EventKind.CREATE, channel="C_SRC", ts="1.1", user="U1", text="hey <@U2> look"
    )
    action = await transformer.transform(evt)
    assert action.text == "hey @Bob look"


async def test_rewrites_channel_and_special_mentions(transformer):
    evt = SourceEvent(
        kind=EventKind.CREATE,
        channel="C_SRC",
        ts="1.1",
        user="U1",
        text="see <#C999|random> <!here>",
    )
    action = await transformer.transform(evt)
    assert action.text == "see #random @here"


async def test_preserves_thread_and_files(transformer):
    f = SourceFile(id="F1", name="a.png", mimetype="image/png", url_private="http://x/a")
    evt = SourceEvent(
        kind=EventKind.CREATE,
        channel="C_SRC",
        ts="2.2",
        user="U1",
        text="re",
        thread_ts="1.1",
        files=[f],
    )
    action = await transformer.transform(evt)
    assert action.source_thread_ts == "1.1"
    assert action.files == [f]


def test_is_actionable():
    assert is_actionable(SourceEvent(kind=EventKind.CREATE, channel="C", ts="1", text="hi"))
    assert not is_actionable(SourceEvent(kind=EventKind.CREATE, channel="C", ts="1", text=""))
    assert is_actionable(SourceEvent(kind=EventKind.DELETE, channel="C", ts="1"))
    f = SourceFile(id="F", name="a", mimetype="", url_private="")
    assert is_actionable(
        SourceEvent(kind=EventKind.CREATE, channel="C", ts="1", text="", files=[f])
    )
