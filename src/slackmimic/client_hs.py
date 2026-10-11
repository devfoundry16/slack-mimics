"""Authenticated client for the restricted HeartStamp workspace.

This talks to Slack's Web API the way the browser client does: an ``xoxc-``
token passed as a bearer/form token, together with the ``d`` cookie. Both are
required — the token alone is rejected.

Almost every endpoint here only reads. The writes (``chat_post_message`` and
the upload methods) are used only by the approval-gated reverse relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

BASE_URL = "https://slack.com/api"

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when Slack rejects our credentials (invalid_auth / not_authed)."""


class SlackApiError(Exception):
    """Raised when a Slack API call returns ``ok: false`` for another reason."""

    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


def require_slack_url(url: str) -> httpx.URL:
    """``url`` parsed, or ValueError unless it is https on slack.com or a subdomain.

    Both clients attach their credentials to every request, so file downloads
    and uploads only go to Slack's own hosts.
    """
    parsed = httpx.URL(url)
    host = parsed.host or ""
    if parsed.scheme != "https" or not (host == "slack.com" or host.endswith(".slack.com")):
        raise ValueError(f"refusing to send credentials outside Slack: {url!r}")
    return parsed


# Slack error codes that mean the credentials are no longer valid.
_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}


class HeartStampClient:
    """Minimal read-only Slack Web API client using session credentials."""

    def __init__(
        self,
        xoxc_token: str,
        d_cookie: str,
        *,
        base_url: str = BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = xoxc_token
        # The cookie value is stored raw; httpx handles header encoding.
        cookie_value = d_cookie if d_cookie.startswith("d=") else f"d={d_cookie}"
        self._cookie = cookie_value
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {xoxc_token}",
                "Cookie": cookie_value,
            },
            timeout=30.0,
        )

    @property
    def ws_headers(self) -> dict[str, str]:
        """Headers for the RTM websocket handshake.

        Session (xoxc) tokens need the ``d`` cookie on every request, including
        the websocket; without it Slack sends an ``error`` event and drops the
        socket within seconds.
        """
        return {"Cookie": self._cookie}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HeartStampClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        """POST to a Slack Web API method and return the parsed body.

        The token is also sent as a form field, matching how the web client
        calls these endpoints.
        """
        data = {k: v for k, v in params.items() if v is not None}
        data.setdefault("token", self._token)
        for _ in range(5):
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
                if error in _AUTH_ERRORS:
                    raise AuthError(
                        f"{method}: {error} — re-extract HS_XOXC_TOKEN and HS_D_COOKIE"
                    )
                if error == "ratelimited":
                    await asyncio.sleep(1.0)
                    continue
                raise SlackApiError(method, error)
            return body
        raise SlackApiError(method, "ratelimited")

    # --- endpoints ---------------------------------------------------------

    async def auth_test(self) -> dict[str, Any]:
        """Verify credentials; returns the resolved identity (user, team, url)."""
        return await self._call("auth.test")

    async def conversations_history(
        self,
        channel: str,
        *,
        oldest: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch channel messages newer than ``oldest`` (exclusive)."""
        return await self._call(
            "conversations.history",
            channel=channel,
            oldest=oldest,
            limit=limit,
            cursor=cursor,
            inclusive="false",
        )

    async def conversations_replies(
        self,
        channel: str,
        ts: str,
        *,
        oldest: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch replies in a thread rooted at ``ts``."""
        return await self._call(
            "conversations.replies",
            channel=channel,
            ts=ts,
            oldest=oldest,
            limit=limit,
            cursor=cursor,
            inclusive="false",
        )

    async def users_list(
        self, *, limit: int = 200, cursor: Optional[str] = None
    ) -> dict[str, Any]:
        """List workspace members (one page)."""
        return await self._call("users.list", limit=limit, cursor=cursor)

    async def conversations_open(self, users: str) -> dict[str, Any]:
        """Open (or find) the session user's DM with ``users``; sends nothing."""
        return await self._call("conversations.open", users=users)

    async def users_info(self, user: str) -> dict[str, Any]:
        """Look up a single user's profile."""
        return await self._call("users.info", user=user)

    async def users_conversations(
        self,
        *,
        types: str = "public_channel,private_channel",
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """List conversations the authenticated user is a member of."""
        return await self._call(
            "users.conversations", types=types, limit=limit, cursor=cursor
        )

    async def rtm_connect(self) -> str:
        """Open a realtime session and return the websocket gateway URL.

        Uses the (deprecated but often still functional) RTM API. Availability
        depends on the workspace; the reader falls back to polling if this
        raises.
        """
        body = await self._call("rtm.connect")
        url = body.get("url")
        if not url:
            raise SlackApiError("rtm.connect", "no_url_in_response")
        return url

    async def chat_post_message(
        self, channel: str, text: str, *, thread_ts: Optional[str] = None
    ) -> str:
        """Post a message into an HS channel or DM *as the session user*.

        Used by the reverse relay to send an approved message to HeartStamp. The
        `xoxc` token posts as the authenticated user (John), not as a bot.
        Returns the new message ts.
        """
        body = await self._call(
            "chat.postMessage", channel=channel, text=text, thread_ts=thread_ts
        )
        return str(body["ts"])

    async def reserve_upload(self, filename: str, content: bytes) -> str:
        """Upload a file's bytes to HS, unshared; returns its file id.

        :meth:`share_uploads` then posts it. Splitting the two lets the relay
        record the id first, so the forward mirror never echoes the message.
        """
        body = await self._call(
            "files.getUploadURLExternal", filename=filename, length=len(content)
        )
        url = require_slack_url(str(body["upload_url"]))
        resp = await self._client.post(url, content=content, follow_redirects=False)
        resp.raise_for_status()
        return str(body["file_id"])

    async def share_uploads(
        self,
        channel: str,
        uploads: list[tuple[str, str]],
        *,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> None:
        """Post reserved uploads ``(file_id, title)`` as one message with ``text``."""
        await self._call(
            "files.completeUploadExternal",
            files=json.dumps([{"id": fid, "title": title} for fid, title in uploads]),
            channel_id=channel,
            initial_comment=text or None,
            thread_ts=thread_ts,
        )

    async def find_file_message(
        self,
        channel: str,
        file_id: str,
        *,
        thread_ts: Optional[str] = None,
        attempts: int = 5,
        delay: float = 1.0,
    ) -> Optional[str]:
        """ts of the message carrying ``file_id``, or None if it doesn't show up.

        Sharing an upload doesn't return the message ts, and the message can
        take a moment to appear.
        """
        for attempt in range(attempts):
            if thread_ts:
                body = await self.conversations_replies(channel, thread_ts, limit=50)
            else:
                body = await self._call("conversations.history", channel=channel, limit=20)
            for msg in body.get("messages", []):
                if any(f.get("id") == file_id for f in msg.get("files") or []):
                    return str(msg["ts"])
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return None

    async def download_file(self, url_private: str) -> bytes:
        """Download a private file using the session credentials.

        The session token and cookie go out with every request, so only https
        URLs on Slack's own hosts are fetched, without following redirects.
        File URLs can come from message attachments, which any API caller in
        HS can fill in.
        """
        url = require_slack_url(url_private)
        resp = await self._client.get(url, follow_redirects=False)
        resp.raise_for_status()
        return resp.content
