"""SQLite-backed state: cursors, the source->target message map, and user cache.

All three tables are what make the daemon resumable and idempotent:

* ``channels`` — last-seen source ``ts`` per channel, so a restart resumes
  without re-mirroring or gaps.
* ``threads`` — newest reply seen per source thread. Separate from the
  channel cursor, which top-level polling moves past replies it can't see.
* ``message_map`` — maps a source ``(channel, ts)`` to the target ``(channel,
  ts)`` it was posted as. Powers threading, edits, and deletes.
* ``users`` — cached id -> display name / avatar to avoid re-fetching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    source_channel TEXT PRIMARY KEY,
    last_ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    source_channel TEXT NOT NULL,
    thread_ts      TEXT NOT NULL,
    last_reply_ts  TEXT NOT NULL,
    PRIMARY KEY (source_channel, thread_ts)
);

CREATE TABLE IF NOT EXISTS message_map (
    source_channel TEXT NOT NULL,
    source_ts      TEXT NOT NULL,
    target_channel TEXT NOT NULL,
    target_ts      TEXT NOT NULL,
    PRIMARY KEY (source_channel, source_ts)
);

-- Reverse lookups (target -> source) for threading relayed replies.
CREATE INDEX IF NOT EXISTS message_map_target
    ON message_map (target_channel, target_ts);

CREATE TABLE IF NOT EXISTS users (
    user_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    icon_url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending_outbound (
    id             TEXT PRIMARY KEY,
    target_channel TEXT NOT NULL,
    target_ts      TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    card_channel   TEXT NOT NULL DEFAULT '',
    card_ts        TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    thread_ts      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reverse_posts (
    source_channel TEXT NOT NULL,
    source_ts      TEXT NOT NULL,
    PRIMARY KEY (source_channel, source_ts)
);
"""


@dataclass(frozen=True)
class CachedUser:
    user_id: str
    name: str
    icon_url: str


@dataclass(frozen=True)
class PendingOutbound:
    id: str
    target_channel: str
    target_ts: str
    source_channel: str
    text: str
    status: str
    card_channel: str
    card_ts: str
    created_at: float
    # vanta-core thread root when the message is a thread reply; "" otherwise.
    thread_ts: str = ""


class StateStore:
    """Async wrapper around the SQLite state database.

    Use as an async context manager, or call :meth:`connect` / :meth:`close`.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> "StateStore":
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()
        return self

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cur = await self._conn.execute("PRAGMA table_info(pending_outbound)")
        columns = {row["name"] for row in await cur.fetchall()}
        if "thread_ts" not in columns:
            await self._conn.execute(
                "ALTER TABLE pending_outbound ADD COLUMN thread_ts TEXT NOT NULL DEFAULT ''"
            )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "StateStore":
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore is not connected; call connect() first.")
        return self._db

    # --- cursors -----------------------------------------------------------

    async def get_last_ts(self, source_channel: str) -> Optional[str]:
        cur = await self._conn.execute(
            "SELECT last_ts FROM channels WHERE source_channel = ?",
            (source_channel,),
        )
        row = await cur.fetchone()
        return row["last_ts"] if row else None

    async def set_last_ts(self, source_channel: str, ts: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO channels (source_channel, last_ts) VALUES (?, ?)
            ON CONFLICT(source_channel) DO UPDATE SET last_ts = excluded.last_ts
            """,
            (source_channel, ts),
        )
        await self._conn.commit()

    # --- thread cursors ----------------------------------------------------

    async def get_thread_cursor(self, source_channel: str, thread_ts: str) -> Optional[str]:
        """Newest reply ``ts`` already read from a source thread, if any."""
        cur = await self._conn.execute(
            "SELECT last_reply_ts FROM threads WHERE source_channel = ? AND thread_ts = ?",
            (source_channel, thread_ts),
        )
        row = await cur.fetchone()
        return row["last_reply_ts"] if row else None

    async def set_thread_cursor(
        self, source_channel: str, thread_ts: str, last_reply_ts: str
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO threads (source_channel, thread_ts, last_reply_ts) VALUES (?, ?, ?)
            ON CONFLICT(source_channel, thread_ts) DO UPDATE SET
                last_reply_ts = excluded.last_reply_ts
            """,
            (source_channel, thread_ts, last_reply_ts),
        )
        await self._conn.commit()

    # --- message map -------------------------------------------------------

    async def record_mapping(
        self,
        source_channel: str,
        source_ts: str,
        target_channel: str,
        target_ts: str,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO message_map
                (source_channel, source_ts, target_channel, target_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_channel, source_ts) DO UPDATE SET
                target_channel = excluded.target_channel,
                target_ts = excluded.target_ts
            """,
            (source_channel, source_ts, target_channel, target_ts),
        )
        await self._conn.commit()

    async def get_target_ts(
        self, source_channel: str, source_ts: str
    ) -> Optional[tuple[str, str]]:
        """Return ``(target_channel, target_ts)`` for a source message, if known."""
        cur = await self._conn.execute(
            """
            SELECT target_channel, target_ts FROM message_map
            WHERE source_channel = ? AND source_ts = ?
            """,
            (source_channel, source_ts),
        )
        row = await cur.fetchone()
        return (row["target_channel"], row["target_ts"]) if row else None

    async def get_source_ts(
        self, target_channel: str, target_ts: str
    ) -> Optional[tuple[str, str]]:
        """Return ``(source_channel, source_ts)`` for a target message, if known."""
        cur = await self._conn.execute(
            """
            SELECT source_channel, source_ts FROM message_map
            WHERE target_channel = ? AND target_ts = ?
            """,
            (target_channel, target_ts),
        )
        row = await cur.fetchone()
        return (row["source_channel"], row["source_ts"]) if row else None

    async def delete_mapping(self, source_channel: str, source_ts: str) -> None:
        await self._conn.execute(
            "DELETE FROM message_map WHERE source_channel = ? AND source_ts = ?",
            (source_channel, source_ts),
        )
        await self._conn.commit()

    # --- user cache --------------------------------------------------------

    async def get_user(self, user_id: str) -> Optional[CachedUser]:
        cur = await self._conn.execute(
            "SELECT user_id, name, icon_url FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return CachedUser(row["user_id"], row["name"], row["icon_url"])

    async def put_user(self, user: CachedUser) -> None:
        await self._conn.execute(
            """
            INSERT INTO users (user_id, name, icon_url) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                icon_url = excluded.icon_url
            """,
            (user.user_id, user.name, user.icon_url),
        )
        await self._conn.commit()

    # --- reverse relay: pending outbound -----------------------------------

    async def create_pending(self, pending: PendingOutbound) -> None:
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO pending_outbound
                (id, target_channel, target_ts, source_channel, text,
                 status, card_channel, card_ts, created_at, thread_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pending.id,
                pending.target_channel,
                pending.target_ts,
                pending.source_channel,
                pending.text,
                pending.status,
                pending.card_channel,
                pending.card_ts,
                pending.created_at,
                pending.thread_ts,
            ),
        )
        await self._conn.commit()

    async def get_pending(self, pending_id: str) -> Optional[PendingOutbound]:
        cur = await self._conn.execute(
            "SELECT * FROM pending_outbound WHERE id = ?", (pending_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return PendingOutbound(
            id=row["id"],
            target_channel=row["target_channel"],
            target_ts=row["target_ts"],
            source_channel=row["source_channel"],
            text=row["text"],
            status=row["status"],
            card_channel=row["card_channel"],
            card_ts=row["card_ts"],
            created_at=row["created_at"],
            thread_ts=row["thread_ts"],
        )

    async def set_pending_card(
        self, pending_id: str, card_channel: str, card_ts: str
    ) -> None:
        await self._conn.execute(
            "UPDATE pending_outbound SET card_channel = ?, card_ts = ? WHERE id = ?",
            (card_channel, card_ts, pending_id),
        )
        await self._conn.commit()

    async def set_pending_status(self, pending_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE pending_outbound SET status = ? WHERE id = ?",
            (status, pending_id),
        )
        await self._conn.commit()

    # --- reverse relay: anti-echo ------------------------------------------

    async def record_reverse_post(self, source_channel: str, source_ts: str) -> None:
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO reverse_posts (source_channel, source_ts)
            VALUES (?, ?)
            """,
            (source_channel, source_ts),
        )
        await self._conn.commit()

    async def is_reverse_post(self, source_channel: str, source_ts: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM reverse_posts WHERE source_channel = ? AND source_ts = ?",
            (source_channel, source_ts),
        )
        return await cur.fetchone() is not None
