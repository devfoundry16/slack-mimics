"""Bot-token client for the target (own) workspace.

Wraps the small set of Slack Web API methods we write with, including the
modern external file-upload flow (``files.getUploadURLExternal`` ->
``completeUploadExternal``), since ``files.upload`` is deprecated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from ..client_hs import SlackApiError

log = logging.getLogger(__name__)

BASE_URL = "https://slack.com/api"


class TargetClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = bot_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=60.0,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TargetClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        data = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            resp = await self._client.post(f"/{method}", data=data)
            if resp.status_code == 429:
                delay = float(resp.headers.get("Retry-After", "1"))
                log.info("%s rate-limited; retrying in %.0fs", method, delay)
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok", False):
                error = body.get("error", "unknown_error")
                if error == "ratelimited":
                    await asyncio.sleep(1.0)
                    continue
                raise SlackApiError(method, error)
            return body
        raise SlackApiError(method, "ratelimited")

    async def auth_test(self) -> dict[str, Any]:
        return await self._call("auth.test")

    async def conversations_list(
        self,
        *,
        types: str = "public_channel,private_channel",
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._call(
            "conversations.list",
            types=types,
            limit=limit,
            cursor=cursor,
            exclude_archived="true",
        )

    async def conversations_create(self, name: str, *, is_private: bool = False) -> dict[str, Any]:
        """Create a channel; the bot is auto-added as a member."""
        return await self._call(
            "conversations.create",
            name=name,
            is_private="true" if is_private else "false",
        )

    async def conversations_join(self, channel: str) -> dict[str, Any]:
        """Join an existing public channel (needs channels:join)."""
        return await self._call("conversations.join", channel=channel)

    async def conversations_invite(self, channel: str, users: str) -> dict[str, Any]:
        """Invite one or more users (comma-separated ids) to a channel."""
        return await self._call("conversations.invite", channel=channel, users=users)

    async def conversations_rename(self, channel: str, name: str) -> dict[str, Any]:
        """Rename a channel."""
        return await self._call("conversations.rename", channel=channel, name=name)

    async def conversations_archive(self, channel: str) -> dict[str, Any]:
        """Archive a channel (reversible removal)."""
        return await self._call("conversations.archive", channel=channel)

    async def lookup_by_email(self, email: str) -> dict[str, Any]:
        """Find a user by email (needs users:read.email)."""
        return await self._call("users.lookupByEmail", email=email)

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> str:
        """Post a message; returns the new message ts."""
        body = await self._call(
            "chat.postMessage",
            channel=channel,
            text=text,
            username=username,
            icon_url=icon_url,
            thread_ts=thread_ts,
        )
        return body["ts"]

    async def update_message(self, channel: str, ts: str, text: str) -> None:
        await self._call("chat.update", channel=channel, ts=ts, text=text)

    async def delete_message(self, channel: str, ts: str) -> None:
        await self._call("chat.delete", channel=channel, ts=ts)

    async def add_reaction(self, channel: str, ts: str, name: str) -> None:
        await self._call("reactions.add", channel=channel, timestamp=ts, name=name)

    async def remove_reaction(self, channel: str, ts: str, name: str) -> None:
        await self._call("reactions.remove", channel=channel, timestamp=ts, name=name)

    async def upload_file(
        self,
        channel: str,
        filename: str,
        content: bytes,
        *,
        thread_ts: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Upload a file via the external-upload flow and share it to a channel."""
        # 1) Reserve an upload URL.
        reserve = await self._call(
            "files.getUploadURLExternal",
            filename=filename,
            length=len(content),
        )
        upload_url = reserve["upload_url"]
        file_id = reserve["file_id"]

        # 2) PUT the bytes to the reserved URL.
        put = await self._client.post(upload_url, content=content)
        put.raise_for_status()

        # 3) Complete + share to the channel.
        files_arg = [{"id": file_id, "title": title or filename}]
        import json as _json

        await self._call(
            "files.completeUploadExternal",
            files=_json.dumps(files_arg),
            channel_id=channel,
            thread_ts=thread_ts,
        )
