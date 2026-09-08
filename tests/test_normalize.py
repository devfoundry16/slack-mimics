from slackmimic.models import EventKind
from slackmimic.source import normalize


def test_plain_message():
    evt = normalize.message_to_event("C1", {"ts": "1.1", "user": "U1", "text": "hi"})
    assert evt is not None
    assert evt.kind == EventKind.CREATE
    assert evt.channel == "C1"
    assert evt.user == "U1"
    assert evt.text == "hi"
    assert evt.thread_ts is None


def test_thread_reply():
    evt = normalize.message_to_event(
        "C1", {"ts": "2.2", "user": "U1", "text": "re", "thread_ts": "1.1"}
    )
    assert evt.thread_ts == "1.1"


def test_ignored_subtype():
    assert normalize.message_to_event("C1", {"ts": "1.1", "subtype": "channel_join"}) is None


def test_message_with_files():
    evt = normalize.message_to_event(
        "C1",
        {
            "ts": "1.1",
            "user": "U1",
            "text": "",
            "files": [
                {"id": "F1", "name": "a.png", "mimetype": "image/png", "url_private": "http://x/a", "size": 10}
            ],
        },
    )
    assert len(evt.files) == 1
    assert evt.files[0].name == "a.png"
    assert evt.files[0].url_private == "http://x/a"


def test_missing_ts_skipped():
    assert normalize.message_to_event("C1", {"user": "U1", "text": "hi"}) is None


def test_rtm_new_message():
    evt = normalize.rtm_event_to_event(
        {"type": "message", "channel": "C1", "ts": "1.1", "user": "U1", "text": "hi"}
    )
    assert evt.kind == EventKind.CREATE
    assert evt.text == "hi"


def test_rtm_message_changed():
    evt = normalize.rtm_event_to_event(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C1",
            "message": {"ts": "1.1", "user": "U1", "text": "edited"},
        }
    )
    assert evt.kind == EventKind.EDIT
    assert evt.ts == "1.1"
    assert evt.text == "edited"


def test_rtm_message_deleted():
    evt = normalize.rtm_event_to_event(
        {"type": "message", "subtype": "message_deleted", "channel": "C1", "deleted_ts": "1.1"}
    )
    assert evt.kind == EventKind.DELETE
    assert evt.ts == "1.1"


def test_rtm_reaction_added():
    evt = normalize.rtm_event_to_event(
        {
            "type": "reaction_added",
            "user": "U1",
            "reaction": "tada",
            "item": {"type": "message", "channel": "C1", "ts": "1.1"},
        }
    )
    assert evt.kind == EventKind.REACTION_ADD
    assert evt.reaction == "tada"
    assert evt.ts == "1.1"


def test_rtm_ignored_event():
    assert normalize.rtm_event_to_event({"type": "user_typing"}) is None
