"""Normalized data models passed between units.

The source reader emits :class:`SourceEvent` objects onto the queue. The
transformer converts them into :class:`TargetAction` objects. Keeping these
plain dataclasses (rather than raw Slack payloads) is what lets each unit be
built and tested independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventKind(str, Enum):
    """Kind of change observed in the source workspace."""

    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    REACTION_ADD = "reaction_add"
    REACTION_REMOVE = "reaction_remove"


@dataclass(frozen=True)
class SourceFile:
    """A file/attachment on a source message."""

    id: str
    name: str
    mimetype: str
    # Authenticated download URL (needs the session token in the Authorization
    # header to fetch). May be empty for files we cannot access.
    url_private: str
    size: int = 0


@dataclass
class SourceEvent:
    """A normalized event observed in the source (HeartStamp) workspace."""

    kind: EventKind
    channel: str
    ts: str
    # Author's Slack user id (may be None for system/bot messages).
    user: Optional[str] = None
    text: str = ""
    # Present when this message is a threaded reply.
    thread_ts: Optional[str] = None
    files: list[SourceFile] = field(default_factory=list)
    # For reaction events.
    reaction: Optional[str] = None
    # Original raw payload, kept for debugging / future fidelity.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TargetAction:
    """A concrete action to perform in the target workspace."""

    kind: EventKind
    # Target channel id (already mapped from the source channel).
    channel: str
    # Correlates back to the source message for the message map.
    source_channel: str
    source_ts: str
    text: str = ""
    username: Optional[str] = None
    icon_url: Optional[str] = None
    # Source thread ts, if this is a reply; resolved to a target ts by the sink.
    source_thread_ts: Optional[str] = None
    files: list[SourceFile] = field(default_factory=list)
    reaction: Optional[str] = None
