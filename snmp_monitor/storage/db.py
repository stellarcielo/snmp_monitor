"""SQLite 接続とスキーマ管理。

標準の :mod:`sqlite3` を単一接続で開き、``asyncio.to_thread`` 経由で扱う。
書き込みは 1 本のロックで直列化しているため、ポーラーと API が同時に触っても
``database is locked`` にならない。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    host           TEXT,
    port           INTEGER,
    tags           TEXT,
    sys_name       TEXT,
    sys_descr      TEXT,
    sys_object_id  TEXT,
    sys_location   TEXT,
    sys_contact    TEXT,
    status         TEXT NOT NULL DEFAULT 'unknown',
    last_error     TEXT,
    uptime_ticks   INTEGER,
    last_poll_at   REAL,
    last_up_at     REAL,
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS interfaces (
    device_id     TEXT NOT NULL,
    if_index      TEXT NOT NULL,
    name          TEXT,
    descr         TEXT,
    alias         TEXT,
    if_type       INTEGER,
    speed_bps     INTEGER,
    mtu           INTEGER,
    mac           TEXT,
    admin_status  INTEGER,
    oper_status   INTEGER,
    last_change   INTEGER,
    updated_at    REAL,
    PRIMARY KEY (device_id, if_index)
);

CREATE TABLE IF NOT EXISTS storages (
    device_id   TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    descr       TEXT,
    kind        TEXT,
    updated_at  REAL,
    PRIMARY KEY (device_id, storage_key)
);

-- res: raw / 5m / 1h
CREATE TABLE IF NOT EXISTS device_metrics (
    device_id       TEXT NOT NULL,
    res             TEXT NOT NULL,
    ts              REAL NOT NULL,
    reachable       INTEGER NOT NULL DEFAULT 0,
    response_ms     REAL,
    cpu_percent     REAL,
    cpu_percent_max REAL,
    mem_used_bytes  INTEGER,
    mem_total_bytes INTEGER,
    uptime_ticks    INTEGER,
    PRIMARY KEY (device_id, res, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS interface_metrics (
    device_id       TEXT NOT NULL,
    if_index        TEXT NOT NULL,
    res             TEXT NOT NULL,
    ts              REAL NOT NULL,
    in_bps          REAL,
    out_bps         REAL,
    in_bps_max      REAL,
    out_bps_max     REAL,
    in_error_rate   REAL,
    out_error_rate  REAL,
    in_discard_rate REAL,
    out_discard_rate REAL,
    oper_status     INTEGER,
    PRIMARY KEY (device_id, if_index, res, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS storage_metrics (
    device_id   TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    res         TEXT NOT NULL,
    ts          REAL NOT NULL,
    used_bytes  INTEGER,
    total_bytes INTEGER,
    PRIMARY KEY (device_id, storage_key, res, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    device_id TEXT NOT NULL,
    if_index  TEXT,
    severity  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_device ON events (device_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_device_metrics_ts ON device_metrics (res, ts);
CREATE INDEX IF NOT EXISTS idx_interface_metrics_ts ON interface_metrics (res, ts);
CREATE INDEX IF NOT EXISTS idx_storage_metrics_ts ON storage_metrics (res, ts);
"""


class Database:
    """非同期から扱える SQLite ラッパ。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() が呼ばれていません")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await asyncio.to_thread(
            sqlite3.connect, str(self.path), 30.0, 0, None, False
        )
        conn.row_factory = sqlite3.Row
        await asyncio.to_thread(self._init_schema, conn)
        self._conn = conn
        logger.info("データベースを開きました: %s", self.path)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        async with self._lock:
            await asyncio.to_thread(conn.close)

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """単一トランザクションとして ``fn`` を実行する。"""

        def _wrapped(conn: sqlite3.Connection) -> T:
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

        async with self._lock:
            return await asyncio.to_thread(_wrapped, self.connection)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.run(lambda conn: conn.execute(sql, params))

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        batch = list(rows)
        if not batch:
            return
        await self.run(lambda conn: conn.executemany(sql, batch))

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self.connection.execute(sql, params).fetchall()
            )

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self.connection.execute(sql, params).fetchone()
            )
