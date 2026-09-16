"""Eligibility rules for reverse relay — pure, so it's unit-testable.

A vanta-core message is a relay candidate if it is a plain message typed by a
human member — the owner or anyone else in a mapped channel. The owner still
approves every one of them, since the relay posts to HeartStamp under the
owner's identity. This deliberately excludes:
- the forward mirror's own posts (they carry ``bot_id``/``app_id``),
- edits/joins/other subtypes, authorless events, and empty messages.
"""

from __future__ import annotations

from typing import Any


def is_eligible(event: dict[str, Any]) -> bool:
    if event.get("bot_id") or event.get("app_id"):
        return False
    if event.get("subtype"):  # message_changed, channel_join, bot_message, …
        return False
    if not str(event.get("user", "")).strip():  # authorless event
        return False
    if not str(event.get("text", "")).strip():
        return False
    return True
