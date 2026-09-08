"""Resolve Slack user ids to display names and avatars, with caching.

Reads through the SQLite user cache first; on a miss, fetches from HS via
``users.info`` and stores the result. A failed lookup degrades to a stable
placeholder rather than raising, so one unknown user never stalls the mirror.
"""

from __future__ import annotations

from typing import Any

from .client_hs import HeartStampClient, SlackApiError
from .state.store import CachedUser, StateStore


class UserResolver:
    def __init__(self, client: HeartStampClient, store: StateStore) -> None:
        self._client = client
        self._store = store

    async def resolve(self, user_id: str) -> CachedUser:
        if not user_id:
            return CachedUser("", "unknown", "")

        cached = await self._store.get_user(user_id)
        if cached is not None:
            return cached

        user = await self._fetch(user_id)
        await self._store.put_user(user)
        return user

    async def _fetch(self, user_id: str) -> CachedUser:
        try:
            body = await self._client.users_info(user_id)
        except SlackApiError:
            # Unknown / inaccessible user — use a stable placeholder.
            return CachedUser(user_id, user_id, "")

        return _user_from_payload(user_id, body.get("user") or {})


def _user_from_payload(user_id: str, user: dict[str, Any]) -> CachedUser:
    profile = user.get("profile") or {}
    name = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user_id
    )
    icon = (
        profile.get("image_192")
        or profile.get("image_72")
        or profile.get("image_48")
        or ""
    )
    return CachedUser(user_id, name, icon)
