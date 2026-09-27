"""Eligibility rules for reverse relay — pure, so it's unit-testable.

A vanta-core message is a relay candidate if it is a plain message typed by a
human member — the owner or anyone else in a mapped channel. The owner still
approves every one of them, since the relay posts to HeartStamp under the
owner's identity. This deliberately excludes:
- the forward mirror's own posts (they carry ``bot_id``/``app_id``),
- edits/joins/other subtypes (except ``thread_broadcast`` replies),
  authorless events, and empty messages.
"""

from __future__ import annotations

from typing import Any


def is_eligible(event: dict[str, Any]) -> bool:
    if event.get("bot_id") or event.get("app_id"):
        return False
    # message_changed, channel_join, bot_message, … — but a thread reply also
    # sent to the channel is still a plain human message.
    subtype = event.get("subtype")
    if subtype and subtype != "thread_broadcast":
        return False
    if not str(event.get("user", "")).strip():  # authorless event
        return False
    if not str(event.get("text", "")).strip():
        return False
    return True
