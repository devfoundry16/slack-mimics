"""Authenticated client for the restricted HeartStamp workspace.

This talks to Slack's Web API the way the browser client does: an ``xoxc-``
token passed as a bearer/form token, together with the ``d`` cookie. Both are
required — the token alone is rejected.

Only read endpoints are used here. Nothing in this module writes to HS.
"""

from __future__ import annotations

import asyncio
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
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {xoxc_token}",
                "Cookie": cookie_value,
            },
            timeout=30.0,
        )

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

    async def download_file(self, url_private: str) -> bytes:
        """Download a private file using the session credentials."""
        resp = await self._client.get(url_private)
        resp.raise_for_status()
        return resp.content
