"""Watch source channels and emit normalized events onto a queue.

Two transports:

* :meth:`SourceReader.poll_once` / :meth:`run_polling` — robust fallback that
  fetches new messages (and thread replies) via ``conversations.history`` since
  the stored cursor. Always works. A *thread rescan* widens the history window
  to ``thread_lookback_days`` so replies to older threads are found too.
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
from collections import OrderedDict
from typing import Optional

from ..client_hs import AuthError, HeartStampClient
from ..config import Config
from ..models import EventKind, SourceEvent
from ..state.store import StateStore
from . import normalize

log = logging.getLogger(__name__)

# How many recently queued top-level messages to remember (see _queued_roots).
_QUEUED_ROOTS_MAX = 5000

# A websocket that drops sooner than this counts as failing (caller falls back
# to polling); a later drop is routine and the caller reconnects right away.
_MIN_HEALTHY_SECONDS = 60.0


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
        # (channel, ts) of top-level messages queued but maybe not yet posted,
        # so _ensure_parent doesn't fetch a parent that's already on its way.
        self._queued_roots: OrderedDict[tuple[str, str], None] = OrderedDict()

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

    async def poll_once(self, rescan_threads: bool = False) -> int:
        """Poll every channel once. Returns the number of events emitted.

        With ``rescan_threads`` the history window reaches back
        ``thread_lookback_days`` so new replies to older threads are found;
        ``conversations.history`` only returns top-level messages, so a reply
        to a parent older than the cursor is otherwise invisible to polling.
        """
        total = 0
        for channel in self._config.source_channels:
            total += await self._poll_channel(channel, rescan_threads)
        return total

    async def _poll_channel(self, channel: str, rescan_threads: bool = False) -> int:
        last_ts = await self._store.get_last_ts(channel)
        oldest = last_ts
        lookback_days = self._config.thread_lookback_days
        if rescan_threads and last_ts is not None and lookback_days > 0:
            lookback = time.time() - lookback_days * 86400
            oldest = f"{min(float(last_ts), lookback):.6f}"
        messages = await self._fetch_history(channel, oldest)
        if not messages:
            return 0

        # Oldest first for correct posting order.
        messages.sort(key=lambda m: float(m.get("ts", 0)))
        newest = last_ts or "0"
        emitted = 0

        for msg in messages:
            ts = str(msg.get("ts", "0"))
            # Messages at or before the cursor were already mirrored; a rescan
            # only returns them to check their threads.
            if last_ts is None or float(ts) > float(last_ts):
                event = normalize.message_to_event(channel, msg)
                if event is not None and await self._emit(event):
                    emitted += 1
                newest = max(newest, ts, key=float)

            # Follow threads with replies we haven't read yet, whether the
            # parent is new or (on a rescan) older than the cursor. Compare
            # against the thread's own cursor, not the channel's: plain polls
            # move the channel cursor past replies they can't see.
            latest_reply = msg.get("latest_reply")
            if latest_reply:
                parent_ts = str(msg["ts"])
                seen = await self._store.get_thread_cursor(channel, parent_ts)
                if seen is None or float(latest_reply) > float(seen):
                    emitted += await self._poll_thread(channel, parent_ts, seen)
                    await self._store.set_thread_cursor(channel, parent_ts, str(latest_reply))
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
            if event is not None and await self._emit(event):
                emitted += 1
        return emitted

    async def _emit(self, event: SourceEvent) -> bool:
        """Queue an event unless it's our own echo. Returns True if queued.

        A reply whose parent was never mirrored gets the parent queued first,
        so the poster can thread the reply under it instead of posting it
        top-level.
        """
        if await self._is_echo(event.channel, event.ts):
            return False
        is_reply = bool(event.thread_ts) and event.thread_ts != event.ts
        # Edits count too: the poster creates an edited message it never saw.
        if event.kind in (EventKind.CREATE, EventKind.EDIT):
            if is_reply:
                await self._ensure_parent(event.channel, str(event.thread_ts))
            else:
                self._remember_root(event.channel, event.ts)
        await self._queue.put(event)
        return True

    async def _ensure_parent(self, channel: str, parent_ts: str) -> None:
        """Queue a thread's parent if it isn't mirrored or already queued."""
        if (channel, parent_ts) in self._queued_roots:
            return
        if await self._store.get_target_ts(channel, parent_ts):
            return
        if await self._is_echo(channel, parent_ts):
            return
        try:
            body = await self._client.conversations_replies(channel, parent_ts, limit=1)
        except AuthError:
            raise
        except Exception as exc:
            log.warning("channel %s: could not fetch thread parent %s: %s", channel, parent_ts, exc)
            return
        parent = next(
            (m for m in body.get("messages", []) if str(m.get("ts")) == parent_ts), None
        )
        event = normalize.message_to_event(channel, parent) if parent else None
        if event is None:
            log.warning("channel %s: thread parent %s not found", channel, parent_ts)
            return
        log.info("channel %s: mirroring thread parent %s before its reply", channel, parent_ts)
        self._remember_root(channel, parent_ts)
        await self._queue.put(event)

    def _remember_root(self, channel: str, ts: str) -> None:
        self._queued_roots[(channel, ts)] = None
        self._queued_roots.move_to_end((channel, ts))
        while len(self._queued_roots) > _QUEUED_ROOTS_MAX:
            self._queued_roots.popitem(last=False)

    async def _is_echo(self, channel: str, ts: str) -> bool:
        """True if this HS message was posted by our own reverse relay.

        Without this, a message John approved into HS would be read back here
        and mirrored into vanta-core, duplicating his original.
        """
        return await self._store.is_reverse_post(channel, ts)

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
        last_rescan = time.monotonic()
        while stop is None or not stop.is_set():
            rescan = time.monotonic() - last_rescan >= self._config.thread_rescan_seconds
            try:
                n = await self.poll_once(rescan_threads=rescan)
                if rescan:
                    last_rescan = time.monotonic()
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
        dependency on it. Raises :class:`AuthError` (propagated) for bad creds.
        Returns when a healthy session drops, so the caller catches up and
        reconnects; raises when the socket fails soon after connecting, so the
        caller falls back to polling instead of reconnecting in a tight loop.
        """
        import websockets

        allowed = set(self._config.source_channels)
        url = await self._client.rtm_connect()
        started = time.monotonic()

        try:
            async with websockets.connect(
                url, additional_headers=self._client.ws_headers
            ) as ws:
                log.info("realtime: connected via websocket")
                await self._stream(ws, allowed, stop)
        except websockets.ConnectionClosed as exc:
            if time.monotonic() - started < _MIN_HEALTHY_SECONDS:
                raise
            log.info("realtime: connection dropped (%s); reconnecting", exc)

    async def _stream(self, ws, allowed: set[str], stop: Optional[asyncio.Event]) -> None:
        while stop is None or not stop.is_set():
            raw = await ws.recv()
            try:
                evt = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if evt.get("type") == "error":
                # e.g. a rejected session; Slack drops the socket right after.
                log.warning("realtime: server error %s", evt.get("error"))
                continue
            event = normalize.rtm_event_to_event(evt)
            if event is None or event.channel not in allowed:
                continue
            if not await self._emit(event):
                continue
            if event.ts:
                prev = await self._store.get_last_ts(event.channel)
                if prev is None or float(event.ts) > float(prev):
                    await self._store.set_last_ts(event.channel, event.ts)
