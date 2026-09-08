"""Watch source channels and emit normalized events onto a queue.

Two transports:

* :meth:`SourceReader.poll_once` / :meth:`run_polling` — robust fallback that
  fetches new messages (and thread replies) via ``conversations.history`` since
  the stored cursor. Always works.
* :meth:`run_websocket` — near-real-time via the RTM websocket, layered on top.
  On any failure it returns so the caller can fall back to polling.

The reader only ever *reads*. Idempotency (not double-posting) is enforced
downstream by the poster via the message map, so overlap between transports is
safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from ..client_hs import AuthError, HeartStampClient
from ..config import Config
from ..models import SourceEvent
from ..state.store import StateStore
from . import normalize

log = logging.getLogger(__name__)


class SourceReader:
    def __init__(
        self,
        client: HeartStampClient,
        config: Config,
        store: StateStore,
        queue: "asyncio.Queue[SourceEvent]",
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._queue = queue

    # --- polling -----------------------------------------------------------

    async def initialize_cursors(self, backfill_days: float = 0.0) -> None:
        """Seed cursors for channels seen for the first time.

        With ``backfill_days > 0`` the cursor starts that many days in the past,
        so the initial poll mirrors recent history; otherwise it starts at *now*
        and only new messages are mirrored. Channels that already have a cursor
        are left untouched, so restarts never re-backfill.
        """
        start = time.time() - backfill_days * 86400 if backfill_days > 0 else time.time()
        start_ts = f"{start:.6f}"
        for channel in self._config.source_channels:
            if await self._store.get_last_ts(channel) is None:
                await self._store.set_last_ts(channel, start_ts)
                when = f"{backfill_days:g}d ago" if backfill_days > 0 else "now"
                log.info("channel %s: starting from %s (%s)", channel, when, start_ts)

    async def poll_once(self) -> int:
        """Poll every channel once. Returns the number of events emitted."""
        total = 0
        for channel in self._config.source_channels:
            total += await self._poll_channel(channel)
        return total

    async def _poll_channel(self, channel: str) -> int:
        last_ts = await self._store.get_last_ts(channel)
        messages = await self._fetch_history(channel, last_ts)
        if not messages:
            return 0

        # Oldest first for correct posting order.
        messages.sort(key=lambda m: float(m.get("ts", 0)))
        newest = last_ts or "0"
        emitted = 0

        for msg in messages:
            event = normalize.message_to_event(channel, msg)
            if event is not None:
                await self._queue.put(event)
                emitted += 1
            newest = max(newest, str(msg.get("ts", "0")), key=float)

            # Follow threads whose replies are newer than our cursor.
            latest_reply = msg.get("latest_reply")
            if latest_reply and (last_ts is None or float(latest_reply) > float(last_ts)):
                emitted += await self._poll_thread(channel, str(msg["ts"]), last_ts)
                newest = max(newest, str(latest_reply), key=float)

        await self._store.set_last_ts(channel, newest)
        return emitted

    async def _poll_thread(
        self, channel: str, parent_ts: str, oldest: Optional[str]
    ) -> int:
        replies = await self._fetch_replies(channel, parent_ts, oldest)
        emitted = 0
        for msg in replies:
            # The parent itself is returned by replies; skip it.
            if str(msg.get("ts")) == parent_ts:
                continue
            event = normalize.message_to_event(channel, msg)
            if event is not None:
                await self._queue.put(event)
                emitted += 1
        return emitted

    async def _fetch_history(self, channel: str, oldest: Optional[str]) -> list[dict]:
        out: list[dict] = []
        cursor: Optional[str] = None
        while True:
            body = await self._client.conversations_history(
                channel, oldest=oldest, cursor=cursor
            )
            out.extend(body.get("messages", []))
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return out

    async def _fetch_replies(
        self, channel: str, parent_ts: str, oldest: Optional[str]
    ) -> list[dict]:
        out: list[dict] = []
        cursor: Optional[str] = None
        while True:
            body = await self._client.conversations_replies(
                channel, parent_ts, oldest=oldest, cursor=cursor
            )
            out.extend(body.get("messages", []))
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return out

    async def run_polling(self, stop: Optional[asyncio.Event] = None) -> None:
        interval = self._config.poll_interval_seconds
        while stop is None or not stop.is_set():
            try:
                n = await self.poll_once()
                if n:
                    log.debug("polled: %d events", n)
            except AuthError:
                raise
            except Exception as exc:
                log.warning("poll error: %s", exc)
            await asyncio.sleep(interval)

    # --- websocket ---------------------------------------------------------

    async def run_websocket(self, stop: Optional[asyncio.Event] = None) -> None:
        """Stream realtime events until the socket drops or ``stop`` is set.

        Import of ``websockets`` is local so the polling path has no hard
        dependency on it. Raises :class:`AuthError` (propagated) for bad creds;
        returns on any other socket failure so the caller can fall back.
        """
        import websockets

        allowed = set(self._config.source_channels)
        url = await self._client.rtm_connect()
        log.info("realtime: connected via websocket")

        async with websockets.connect(url) as ws:
            while stop is None or not stop.is_set():
                raw = await ws.recv()
                try:
                    evt = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                event = normalize.rtm_event_to_event(evt)
                if event is None or event.channel not in allowed:
                    continue
                await self._queue.put(event)
                if event.ts:
                    prev = await self._store.get_last_ts(event.channel)
                    if prev is None or float(event.ts) > float(prev):
                        await self._store.set_last_ts(event.channel, event.ts)
