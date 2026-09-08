"""SQLite-backed state: cursors, the source->target message map, and user cache.

All three tables are what make the daemon resumable and idempotent:

* ``channels`` — last-seen source ``ts`` per channel, so a restart resumes
  without re-mirroring or gaps.
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

CREATE TABLE IF NOT EXISTS message_map (
    source_channel TEXT NOT NULL,
    source_ts      TEXT NOT NULL,
    target_channel TEXT NOT NULL,
    target_ts      TEXT NOT NULL,
    PRIMARY KEY (source_channel, source_ts)
);

CREATE TABLE IF NOT EXISTS users (
    user_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    icon_url TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class CachedUser:
    user_id: str
    name: str
    icon_url: str


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
        await self._db.commit()
        return self

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
