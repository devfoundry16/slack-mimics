import asyncio

import pytest

from slackmimic.config import ChannelMap, Config, Secrets
from slackmimic.reverse import cards
from slackmimic.reverse.detector import is_eligible
from slackmimic.source.reader import SourceReader
from slackmimic.state.store import PendingOutbound, StateStore

OWNER = "U_OWNER"


def make_config(**kw):
    return Config(
        secrets=Secrets("x", "d", "b"),
        channels=[ChannelMap("C_SRC", "C_DST", "general")],
        owner_member_id=OWNER,
        **kw,
    )


# --- eligibility -----------------------------------------------------------

def _msg(**kw):
    base = {"user": OWNER, "text": "hello", "channel": "C_DST", "ts": "1.1"}
    base.update(kw)
    return base


def test_eligible_owner_plain_message():
    assert is_eligible(_msg(), OWNER) is True


def test_reject_bot_and_app_messages():
    assert is_eligible(_msg(bot_id="B1"), OWNER) is False
    assert is_eligible(_msg(app_id="A1"), OWNER) is False


def test_reject_subtypes():
    assert is_eligible(_msg(subtype="message_changed"), OWNER) is False
    assert is_eligible(_msg(subtype="channel_join"), OWNER) is False


def test_reject_other_user_and_empty():
    assert is_eligible(_msg(user="U_OTHER"), OWNER) is False
    assert is_eligible(_msg(text="   "), OWNER) is False
    assert is_eligible(_msg(), "") is False  # no owner configured


# --- config inversion ------------------------------------------------------

def test_source_for_and_label():
    cfg = make_config()
    assert cfg.source_for("C_DST") == "C_SRC"
    assert cfg.source_for("C_UNKNOWN") is None
    assert cfg.label_for_target("C_DST") == "general"


# --- cards -----------------------------------------------------------------

def _pending():
    return PendingOutbound(
        id="p1", target_channel="C_DST", target_ts="1.1", source_channel="C_SRC",
        text="hi there", status="pending", card_channel="", card_ts="", created_at=0.0,
    )


def test_pending_card_has_review_and_discard():
    blocks = cards.pending_card_blocks(_pending(), "general")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    ids = {e["action_id"]: e["value"] for e in actions["elements"]}
    assert ids == {cards.ACTION_REVIEW: "p1", cards.ACTION_DISCARD: "p1"}


def test_review_modal_prefilled_and_ids():
    view = cards.review_modal_view(_pending(), "general")
    assert view["callback_id"] == cards.MODAL_CALLBACK
    assert view["private_metadata"] == "p1"
    block = [b for b in view["blocks"] if b.get("block_id") == cards.MODAL_BLOCK][0]
    assert block["element"]["action_id"] == cards.MODAL_INPUT
    assert block["element"]["initial_value"] == "hi there"


def test_resolved_card_text():
    assert "Sent" in cards.resolved_card_blocks("general", "x", sent=True)[0]["text"]["text"]
    assert "Discarded" in cards.resolved_card_blocks("general", "x", sent=False)[0]["text"]["text"]


# --- store -----------------------------------------------------------------

@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "s.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def test_pending_roundtrip(store):
    p = _pending()
    await store.create_pending(p)
    assert await store.get_pending("p1") == p
    await store.set_pending_card("p1", "D1", "9.9")
    await store.set_pending_status("p1", "sent")
    got = await store.get_pending("p1")
    assert got.card_channel == "D1" and got.card_ts == "9.9" and got.status == "sent"


async def test_reverse_post_roundtrip(store):
    assert await store.is_reverse_post("C_SRC", "5.5") is False
    await store.record_reverse_post("C_SRC", "5.5")
    assert await store.is_reverse_post("C_SRC", "5.5") is True


# --- anti-echo in the forward reader ---------------------------------------

class FakeHS:
    def __init__(self, messages):
        self._messages = messages

    async def conversations_history(self, channel, *, oldest=None, limit=100, cursor=None):
        return {"messages": self._messages}


@pytest.fixture
async def store2(tmp_path):
    s = StateStore(str(tmp_path / "s2.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def test_reader_skips_reverse_posts(store2):
    cfg = make_config()
    q: asyncio.Queue = asyncio.Queue()
    msg = {"ts": "100.1", "user": "U1", "text": "echo"}
    reader = SourceReader(FakeHS([msg]), cfg, store2, q)
    await store2.set_last_ts("C_SRC", "50.0")
    await store2.record_reverse_post("C_SRC", "100.1")  # mark as our own post

    emitted = await reader.poll_once()
    assert emitted == 0
    assert q.empty()


async def test_reader_emits_non_echo(store2):
    cfg = make_config()
    q: asyncio.Queue = asyncio.Queue()
    msg = {"ts": "100.1", "user": "U1", "text": "real"}
    reader = SourceReader(FakeHS([msg]), cfg, store2, q)
    await store2.set_last_ts("C_SRC", "50.0")

    emitted = await reader.poll_once()
    assert emitted == 1
    assert not q.empty()
