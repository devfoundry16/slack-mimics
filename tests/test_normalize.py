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


def _share(**extra):
    att = {
        "is_share": True,
        "author_id": "U9",
        "author_name": "Bea",
        "footer": "Posted in a private conversation",
        "text": "line one\nline two",
        "fallback": "[Oct 10th] Bea: line one",
    }
    att.update(extra)
    return att


def test_forwarded_message_without_comment_renders_quote():
    evt = normalize.message_to_event(
        "C1", {"ts": "1.1", "user": "U1", "text": "", "attachments": [_share()]}
    )
    assert evt.text == (
        ">*Forwarded from <@U9>* · _Posted in a private conversation_\n"
        ">line one\n"
        ">line two"
    )


def test_forwarded_message_keeps_comment_above_quote():
    evt = normalize.message_to_event(
        "C1", {"ts": "1.1", "user": "U1", "text": "look", "attachments": [_share()]}
    )
    assert evt.text.startswith("look\n>*Forwarded from <@U9>*")


def test_forwarded_message_falls_back_to_author_name_and_fallback_text():
    att = _share(author_id=None, footer=None, text="")
    evt = normalize.message_to_event("C1", {"ts": "1.1", "text": "", "attachments": [att]})
    assert evt.text == ">*Forwarded from Bea*\n>[Oct 10th] Bea: line one"


def test_forwarded_message_files_are_mirrored():
    att = _share(files=[{"id": "F2", "name": "b.png", "url_private": "http://x/b"}])
    evt = normalize.message_to_event("C1", {"ts": "1.1", "text": "", "attachments": [att]})
    assert [f.name for f in evt.files] == ["b.png"]


def test_message_link_unfurl_is_rendered():
    att = _share(is_share=None, is_msg_unfurl=True)
    evt = normalize.message_to_event("C1", {"ts": "1.1", "text": "", "attachments": [att]})
    assert ">line one" in evt.text


def test_link_unfurl_attachment_is_ignored():
    att = {"title": "Example", "title_link": "https://example.com", "text": "preview"}
    evt = normalize.message_to_event(
        "C1", {"ts": "1.1", "text": "<https://example.com>", "attachments": [att]}
    )
    assert evt.text == "<https://example.com>"


def test_rtm_message_changed_renders_forward():
    evt = normalize.rtm_event_to_event(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C1",
            "message": {"ts": "1.1", "user": "U1", "text": "", "attachments": [_share()]},
        }
    )
    assert evt.kind == EventKind.EDIT
    assert ">line two" in evt.text
