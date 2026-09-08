"""Recreate the source workspace's channels in the target and emit config.yaml.

For every channel the source user belongs to, ensure a matching channel exists
in the target workspace (reusing a same-named one if present, else creating it),
then write the source->target mapping to a config file.

Kept import-safe (no side effects at import time) so it can be unit-tested; the
CLI wrapper lives in ``scripts/provision.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .client_hs import HeartStampClient, SlackApiError
from .sink.client_target import TargetClient

log = logging.getLogger(__name__)

_NAME_CLEAN = re.compile(r"[^a-z0-9_-]+")


def normalize_channel_name(name: str) -> str:
    """Coerce a name to Slack's channel-name rules (lowercase, <=80, limited set)."""
    name = name.lower().replace(" ", "-").replace(".", "-")
    name = _NAME_CLEAN.sub("-", name).strip("-")
    return name[:80] or "channel"


def short_group_name(
    long_name: str, channel_id: str, *, self_users: tuple[str, ...] = ("john.oliveira",)
) -> str:
    """Turn a long ``mpdm-a--b--c-1`` name into a short, unique ``gdm-…`` name.

    Uses up to three participant first names (excluding yourself), plus a short
    suffix from the channel id to keep it unique when many groups share the same
    first few people.
    """
    base = long_name
    if base.startswith("mpdm-"):
        base = base[len("mpdm-") :]
    # Drop a trailing "-<n>" that Slack appends to mpdm names.
    base = re.sub(r"-\d+$", "", base)
    members = [m for m in base.split("--") if m and m not in self_users]
    firsts = [m.split(".")[0].split("-")[0] for m in members]
    picked = "-".join(firsts[:3]) if firsts else "group"
    suffix = channel_id[-4:].lower()
    return normalize_channel_name(f"gdm-{picked}-{suffix}")


@dataclass
class SourceChannel:
    id: str
    name: str
    is_private: bool


@dataclass
class ProvisionResult:
    # source_id -> (target_id, name, note)
    mappings: list[tuple[str, str, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    needs_invite: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


async def list_source_channels(
    hs: HeartStampClient, *, include_private: bool
) -> list[SourceChannel]:
    types = "public_channel,private_channel" if include_private else "public_channel"
    out: list[SourceChannel] = []
    cursor: Optional[str] = None
    while True:
        body = await hs.users_conversations(types=types, cursor=cursor)
        for ch in body.get("channels", []):
            out.append(
                SourceChannel(
                    id=str(ch["id"]),
                    name=str(ch.get("name", ch["id"])),
                    is_private=bool(ch.get("is_private")),
                )
            )
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


async def list_target_index(target: TargetClient) -> dict[str, dict]:
    """Map existing target channel name -> the channel object."""
    index: dict[str, dict] = {}
    cursor: Optional[str] = None
    while True:
        body = await target.conversations_list(cursor=cursor)
        for ch in body.get("channels", []):
            index[str(ch.get("name"))] = ch
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return index


async def _display_name(hs: HeartStampClient, user_id: str) -> str:
    try:
        body = await hs.users_info(user_id)
    except SlackApiError:
        return user_id
    user = body.get("user") or {}
    profile = user.get("profile") or {}
    return (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user_id
    )


async def list_source_dms(
    hs: HeartStampClient, *, exclude_user_names: list[str] | None = None
) -> list[SourceChannel]:
    """List DMs (im) and group DMs (mpim), naming targets after participants.

    1:1 DMs whose counterpart's name contains any of ``exclude_user_names``
    (case-insensitive) are skipped. Group DMs are always included, even if such
    a person is a member.
    """
    exclude = [x.lower() for x in (exclude_user_names or [])]
    out: list[SourceChannel] = []
    cursor: Optional[str] = None
    while True:
        body = await hs.users_conversations(types="im,mpim", cursor=cursor)
        for ch in body.get("channels", []):
            cid = str(ch["id"])
            if ch.get("is_im"):
                other = ch.get("user")
                name = await _display_name(hs, other) if other else cid
                if any(x in name.lower() for x in exclude):
                    log.info("skipping DM with %s (excluded)", name)
                    continue
                target_name = f"dm-{name}"
            else:  # mpim / group DM
                target_name = str(ch.get("name") or cid)
            out.append(SourceChannel(id=cid, name=target_name, is_private=True))
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


async def provision(
    hs: HeartStampClient,
    target: TargetClient,
    *,
    include_private: bool = True,
    prefix: str = "",
    throttle_seconds: float = 1.0,
) -> ProvisionResult:
    sources = await list_source_channels(hs, include_private=include_private)
    return await provision_sources(
        target, sources, prefix=prefix, throttle_seconds=throttle_seconds
    )


async def provision_sources(
    target: TargetClient,
    sources: list[SourceChannel],
    *,
    existing: Optional[dict[str, dict]] = None,
    prefix: str = "",
    throttle_seconds: float = 1.0,
) -> ProvisionResult:
    result = ProvisionResult()
    if existing is None:
        existing = await list_target_index(target)

    for src in sources:
        target_name = normalize_channel_name(f"{prefix}{src.name}")
        try:
            if target_name in existing:
                ch = existing[target_name]
                target_id = str(ch["id"])
                note = "reused"
                if ch.get("is_private"):
                    # Bot may not be a member of a pre-existing private channel.
                    if not ch.get("is_member", False):
                        result.needs_invite.append(target_name)
                        note = "reused (needs /invite)"
                else:
                    try:
                        await target.conversations_join(target_id)
                    except SlackApiError:
                        pass  # already a member, or cannot join
                result.reused.append(target_name)
            else:
                created = await target.conversations_create(
                    target_name, is_private=src.is_private
                )
                target_id = str(created["channel"]["id"])
                existing[target_name] = created["channel"]
                result.created.append(target_name)
                note = "created"
                await asyncio.sleep(throttle_seconds)

            result.mappings.append((src.id, target_id, src.name))
            log.info("%-40s -> %s (%s)", f"#{src.name}", target_id, note)
        except SlackApiError as exc:
            result.failed.append((src.name, exc.error))
            log.warning("failed #%s: %s", src.name, exc.error)

    return result


def render_config_yaml(
    mappings: list[tuple[str, str, str]],
    *,
    poll_interval_seconds: float = 5.0,
    use_websocket: bool = True,
    db_path: str = "slackmimic.sqlite3",
    backfill_days: float = 0.0,
) -> str:
    lines = [
        f"db_path: {db_path}",
        f"poll_interval_seconds: {poll_interval_seconds}",
        f"use_websocket: {str(use_websocket).lower()}",
        f"backfill_days: {backfill_days:g}",
        "",
        "channels:",
    ]
    for source_id, target_id, label in mappings:
        lines.append(f"  - source: {source_id}")
        lines.append(f"    target: {target_id}")
        lines.append(f"    label: {label}")
    return "\n".join(lines) + "\n"
