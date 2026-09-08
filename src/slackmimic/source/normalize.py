"""Convert raw Slack payloads into normalized :class:`SourceEvent` objects.

Kept as pure functions so they can be unit-tested against recorded payloads
without any network or async machinery.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import EventKind, SourceEvent, SourceFile

# Message subtypes that are noise we never mirror.
_IGNORED_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "bot_add",
    "bot_remove",
}


def _files_from(msg: dict[str, Any]) -> list[SourceFile]:
    files: list[SourceFile] = []
    for f in msg.get("files") or []:
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private") or ""
        files.append(
            SourceFile(
                id=str(f.get("id", "")),
                name=str(f.get("name") or f.get("title") or "file"),
                mimetype=str(f.get("mimetype", "")),
                url_private=url,
                size=int(f.get("size", 0) or 0),
            )
        )
    return files


def message_to_event(channel: str, msg: dict[str, Any]) -> Optional[SourceEvent]:
    """Normalize a message from ``conversations.history`` / ``.replies``.

    Returns ``None`` for messages that should be skipped (ignored subtypes,
    tombstones, etc.).
    """
    subtype = msg.get("subtype")
    if subtype in _IGNORED_SUBTYPES:
        return None

    ts = msg.get("ts")
    if not ts:
        return None

    return SourceEvent(
        kind=EventKind.CREATE,
        channel=channel,
        ts=str(ts),
        user=msg.get("user") or msg.get("bot_id"),
        text=str(msg.get("text", "")),
        thread_ts=str(msg["thread_ts"]) if msg.get("thread_ts") else None,
        files=_files_from(msg),
        raw=msg,
    )


def rtm_event_to_event(evt: dict[str, Any]) -> Optional[SourceEvent]:
    """Normalize a realtime websocket event.

    Handles new messages, edits (``message_changed``), deletes
    (``message_deleted``), and reactions.
    """
    etype = evt.get("type")

    if etype == "message":
        subtype = evt.get("subtype")

        if subtype == "message_changed":
            inner = evt.get("message") or {}
            return SourceEvent(
                kind=EventKind.EDIT,
                channel=str(evt.get("channel", "")),
                ts=str(inner.get("ts", "")),
                user=inner.get("user"),
                text=str(inner.get("text", "")),
                thread_ts=str(inner["thread_ts"]) if inner.get("thread_ts") else None,
                files=_files_from(inner),
                raw=evt,
            )

        if subtype == "message_deleted":
            return SourceEvent(
                kind=EventKind.DELETE,
                channel=str(evt.get("channel", "")),
                ts=str(evt.get("deleted_ts", "")),
                raw=evt,
            )

        if subtype in _IGNORED_SUBTYPES:
            return None

        # Plain new message.
        return message_to_event(str(evt.get("channel", "")), evt)

    if etype in ("reaction_added", "reaction_removed"):
        item = evt.get("item") or {}
        if item.get("type") != "message":
            return None
        kind = (
            EventKind.REACTION_ADD
            if etype == "reaction_added"
            else EventKind.REACTION_REMOVE
        )
        return SourceEvent(
            kind=kind,
            channel=str(item.get("channel", "")),
            ts=str(item.get("ts", "")),
            user=evt.get("user"),
            reaction=evt.get("reaction"),
            raw=evt,
        )

    return None
