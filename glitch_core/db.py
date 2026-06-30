from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """Thin async wrapper over a single aiosqlite connection (single-user app).

    WAL mode + foreign keys on. One long-lived connection is fine for one user;
    aiosqlite serializes statements on its own worker thread.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()
        logger.info("SQLite connected: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executescript(self, script: str) -> None:
        await self.conn.executescript(script)
        await self.conn.commit()

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row is not None else None
