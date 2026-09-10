"""Eligibility rules for reverse relay — pure, so it's unit-testable.

A vanta-core message is a relay candidate only if it is a plain message the owner
themselves typed. This deliberately excludes:
- the forward mirror's own posts (they carry ``bot_id``/``app_id``),
- non-owner members' messages,
- edits/joins/other subtypes and empty messages.
"""

from __future__ import annotations

from typing import Any


def is_eligible(event: dict[str, Any], owner_member_id: str) -> bool:
    if event.get("bot_id") or event.get("app_id"):
        return False
    if event.get("subtype"):  # message_changed, channel_join, bot_message, …
        return False
    if not owner_member_id or event.get("user") != owner_member_id:
        return False
    if not str(event.get("text", "")).strip():
        return False
    return True
