"""Eligibility rules for reverse relay — pure, so it's unit-testable.

A vanta-core message is a relay candidate if it is a plain message typed by a
human member — the owner or anyone else in a mapped channel. The owner still
approves every one of them, since the relay posts to HeartStamp under the
owner's identity. This deliberately excludes:
- the forward mirror's own posts (they carry ``bot_id``/``app_id``),
- edits/joins/other subtypes (except ``thread_broadcast`` replies and
  ``file_share`` messages, sent with their files),
  authorless events, and messages with neither text nor files.
"""

from __future__ import annotations

from typing import Any

_HUMAN_SUBTYPES = {"thread_broadcast", "file_share"}


def is_eligible(event: dict[str, Any]) -> bool:
    if event.get("bot_id") or event.get("app_id"):
        return False
    # message_changed, channel_join, bot_message, … — but a thread reply also
    # sent to the channel, or a message with files attached, is still a plain
    # human message.
    subtype = event.get("subtype")
    if subtype and subtype not in _HUMAN_SUBTYPES:
        return False
    if not str(event.get("user", "")).strip():  # authorless event
        return False
    if not str(event.get("text", "")).strip() and not event.get("files"):
        return False
    return True
