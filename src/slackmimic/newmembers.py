"""Give each new HeartStamp member a DM mirror channel in the target workspace.

When someone joins HS — seen live as a ``team_join`` event, or at startup by
comparing ``users.list`` with the members already recorded — open the owner's
DM with them in HS (this sends nothing), create a private ``dm-<name>`` channel
in the target, invite ``new_member_dm_invites`` plus the owner, and start
mirroring it without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .client_hs import AuthError, HeartStampClient, SlackApiError
from .config import ChannelMap, Config, append_channel_mapping
from .provision import SourceChannel, normalize_channel_name, provision_sources
from .sink.client_target import TargetClient
from .state.store import StateStore

log = logging.getLogger(__name__)

# Invite errors that just mean "nothing to do".
_BENIGN_INVITE_ERRORS = {"already_in_channel", "cant_invite_self"}


def _is_person(user: dict[str, Any]) -> bool:
    return not (
        user.get("is_bot")
        or user.get("is_app_user")
        or user.get("deleted")
        or user.get("id") == "USLACKBOT"
    )


def _member_name(user: dict[str, Any]) -> str:
    profile = user.get("profile") or {}
    return str(
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id")
    )


class NewMemberProvisioner:
    def __init__(
        self,
        hs: HeartStampClient,
        target: TargetClient,
        store: StateStore,
        config: Config,
        config_path: str,
    ) -> None:
        self._hs = hs
        self._target = target
        self._store = store
        self._config = config
        self._config_path = config_path
        # One at a time: each run reads and appends to config.yaml.
        self._lock = asyncio.Lock()

    async def reconcile(self) -> int:
        """Handle members who joined while the service was down.

        The first run only records the current members. Returns how many new
        members got a channel.
        """
        members = await self._list_members()
        if not await self._store.has_known_users():
            await self._store.add_known_users([str(m["id"]) for m in members])
            log.info("new-member DMs: recorded %d existing HeartStamp members", len(members))
            return 0
        provisioned = 0
        for user in members:
            if not await self._store.is_known_user(str(user["id"])):
                provisioned += await self.on_member_joined(user)
        return provisioned

    async def on_member_joined(self, user: dict[str, Any]) -> bool:
        """Create the DM mirror for a new member. Returns True if one was set up.

        Failures are logged and the member stays unrecorded, so the next
        startup tries again.
        """
        user_id = str(user.get("id") or "")
        if not user_id:
            return False
        if not _is_person(user):
            await self._store.add_known_users([user_id])
            return False
        async with self._lock:
            try:
                created = await self._provision(user_id, _member_name(user))
            except AuthError:
                raise
            except Exception as exc:
                log.warning(
                    "new-member DM for %s failed (retried on next start): %s", user_id, exc
                )
                return False
        await self._store.add_known_users([user_id])
        return created

    async def _provision(self, user_id: str, name: str) -> bool:
        opened = await self._hs.conversations_open(user_id)
        dm_id = str(opened["channel"]["id"])
        if self._config.target_for(dm_id):
            log.info("new-member DMs: DM %s with %s is already mirrored", dm_id, name)
            return False

        label = self._unique_label(f"dm-{name}", user_id)
        result = await provision_sources(
            self._target,
            [SourceChannel(id=dm_id, name=label, is_private=True)],
            throttle_seconds=0,
        )
        if result.failed:
            raise RuntimeError(f"creating #{label}: {result.failed[0][1]}")
        if result.needs_invite:
            raise RuntimeError(f"#{label} exists as a private channel the bot isn't in")
        _, target_id, label = result.mappings[0]

        await self._invite(target_id)
        # Mirror from now on only; without a cursor the first poll would
        # copy the DM's whole history.
        await self._store.set_last_ts(dm_id, f"{time.time():.6f}")
        append_channel_mapping(self._config_path, dm_id, target_id, label)
        self._config.add_channel(ChannelMap(dm_id, target_id, label))
        log.info("new-member DMs: mirroring DM with %s into #%s (%s)", name, label, target_id)
        return True

    def _unique_label(self, label: str, user_id: str) -> str:
        """Avoid reusing another mirror's channel for a same-named person."""
        taken = {normalize_channel_name(cm.label) for cm in self._config.channels if cm.label}
        if normalize_channel_name(label) in taken:
            return f"{label}-{user_id[-4:].lower()}"
        return label

    async def _invite(self, channel: str) -> None:
        invitees = [*self._config.new_member_dm_invites, self._config.owner_member_id]
        for user in dict.fromkeys(u for u in invitees if u):
            try:
                await self._target.conversations_invite(channel, user)
            except SlackApiError as exc:
                if exc.error not in _BENIGN_INVITE_ERRORS:
                    raise

    async def _list_members(self) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            body = await self._hs.users_list(cursor=cursor)
            members.extend(m for m in body.get("members", []) if m.get("id"))
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return members
