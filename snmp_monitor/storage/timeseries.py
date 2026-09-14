"""時系列データの書き込み・読み出し・集約。"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from typing import Any

from ..config import DeviceConfig, StorageConfig
from ..models import DeviceSnapshot, Event
from .db import Database

logger = logging.getLogger(__name__)

RAW = "raw"
RES_5M = "5m"
RES_1H = "1h"

BUCKET_SECONDS = {RES_5M: 300, RES_1H: 3600}

#: 解像度を選ぶしきい値 (表示期間の秒数, 解像度)
_RESOLUTION_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (6 * 3600, RAW),
    (7 * 86400, RES_5M),
)


def choose_resolution(span_seconds: float) -> str:
    """表示期間に対して適切な解像度を選ぶ。"""
    for limit, res in _RESOLUTION_THRESHOLDS:
        if span_seconds <= limit:
            return res
    return RES_1H


class TimeSeriesStore:
    """ポーリング結果の永続化と参照を担当する。"""

    def __init__(self, db: Database, config: StorageConfig | None = None) -> None:
        self.db = db
        self.config = config or StorageConfig()

    # -- 書き込み --------------------------------------------------------
    async def register_device(self, device: DeviceConfig) -> None:
        """設定上のデバイスを devices テーブルに登録する (初回のみ)。"""
        await self.db.execute(
            """
            INSERT INTO devices (id, name, host, port, tags, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                host = excluded.host,
                port = excluded.port,
                tags = excluded.tags,
                updated_at = excluded.updated_at
            """,
            (
                device.id,
                device.display_name,
                device.host,
                device.port,
                json.dumps(device.tags, ensure_ascii=False),
                time.time(),
            ),
        )

    async def save_snapshot(self, snapshot: DeviceSnapshot) -> None:
        """1 回分のポーリング結果をまとめて 1 トランザクションで書き込む。"""

        def _write(conn: sqlite3.Connection) -> None:
            self._update_device_row(conn, snapshot)
            if snapshot.interfaces:
                self._upsert_interfaces(conn, snapshot)
            self._insert_device_metric(conn, snapshot)
            self._insert_interface_metrics(conn, snapshot)
            self._insert_storage_metrics(conn, snapshot)

        await self.db.run(_write)

    @staticmethod
    def _update_device_row(conn: sqlite3.Connection, s: DeviceSnapshot) -> None:
        status = "up" if s.reachable else "down"
        conn.execute(
            """
            INSERT INTO devices (id, status, last_error, uptime_ticks, last_poll_at,
                                 last_up_at, sys_name, sys_descr, sys_object_id,
                                 sys_location, sys_contact, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status        = excluded.status,
                last_error    = excluded.last_error,
                uptime_ticks  = COALESCE(excluded.uptime_ticks, devices.uptime_ticks),
                last_poll_at  = excluded.last_poll_at,
                last_up_at    = COALESCE(excluded.last_up_at, devices.last_up_at),
                sys_name      = COALESCE(excluded.sys_name, devices.sys_name),
                sys_descr     = COALESCE(excluded.sys_descr, devices.sys_descr),
                sys_object_id = COALESCE(excluded.sys_object_id, devices.sys_object_id),
                sys_location  = COALESCE(excluded.sys_location, devices.sys_location),
                sys_contact   = COALESCE(excluded.sys_contact, devices.sys_contact),
                updated_at    = excluded.updated_at
            """,
            (
                s.device_id,
                status,
                s.error,
                s.uptime_ticks,
                s.ts,
                s.ts if s.reachable else None,
                s.sys_name,
                s.sys_descr,
                s.sys_object_id,
                s.sys_location,
                s.sys_contact,
                s.ts,
            ),
        )

    @staticmethod
    def _upsert_interfaces(conn: sqlite3.Connection, s: DeviceSnapshot) -> None:
        oper = {sample.if_index: sample.oper_status for sample in s.interface_samples}
        rows = [
            (
                s.device_id,
                i.if_index,
                i.name,
                i.descr,
                i.alias,
                i.if_type,
                i.speed_bps,
                i.mtu,
                i.mac,
                i.admin_status,
                oper.get(i.if_index),
                i.last_change,
                s.ts,
            )
            for i in (s.interfaces or [])
        ]
        conn.executemany(
            """
            INSERT INTO interfaces (device_id, if_index, name, descr, alias, if_type,
                                    speed_bps, mtu, mac, admin_status, oper_status,
                                    last_change, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, if_index) DO UPDATE SET
                name         = excluded.name,
                descr        = excluded.descr,
                alias        = excluded.alias,
                if_type      = excluded.if_type,
                speed_bps    = excluded.speed_bps,
                mtu          = excluded.mtu,
                mac          = excluded.mac,
                admin_status = excluded.admin_status,
                oper_status  = COALESCE(excluded.oper_status, interfaces.oper_status),
                last_change  = excluded.last_change,
                updated_at   = excluded.updated_at
            """,
            rows,
        )

    @staticmethod
    def _insert_device_metric(conn: sqlite3.Connection, s: DeviceSnapshot) -> None:
        conn.execute(
            """
            INSERT INTO device_metrics (device_id, res, ts, reachable, response_ms,
                                        cpu_percent, cpu_percent_max, mem_used_bytes,
                                        mem_total_bytes, uptime_ticks)
            VALUES (?, 'raw', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, res, ts) DO UPDATE SET
                reachable       = excluded.reachable,
                response_ms     = excluded.response_ms,
                cpu_percent     = excluded.cpu_percent,
                cpu_percent_max = excluded.cpu_percent_max,
                mem_used_bytes  = excluded.mem_used_bytes,
                mem_total_bytes = excluded.mem_total_bytes,
                uptime_ticks    = excluded.uptime_ticks
            """,
            (
                s.device_id,
                s.ts,
                1 if s.reachable else 0,
                s.response_ms,
                s.cpu_percent,
                s.cpu_percent,
                s.mem_used_bytes,
                s.mem_total_bytes,
                s.uptime_ticks,
            ),
        )

    @staticmethod
    def _insert_interface_metrics(conn: sqlite3.Connection, s: DeviceSnapshot) -> None:
        rows = [
            (
                s.device_id,
                sample.if_index,
                s.ts,
                sample.in_bps,
                sample.out_bps,
                sample.in_bps,
                sample.out_bps,
                sample.in_error_rate,
                sample.out_error_rate,
                sample.in_discard_rate,
                sample.out_discard_rate,
                sample.oper_status,
            )
            for sample in s.interface_samples
        ]
        conn.executemany(
            """
            INSERT INTO interface_metrics (device_id, if_index, res, ts, in_bps, out_bps,
                                           in_bps_max, out_bps_max, in_error_rate,
                                           out_error_rate, in_discard_rate,
                                           out_discard_rate, oper_status)
            VALUES (?, ?, 'raw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, if_index, res, ts) DO NOTHING
            """,
            rows,
        )
        # 最新の oper_status を interfaces にも反映しておく (一覧表示用)
        conn.executemany(
            "UPDATE interfaces SET oper_status = ?, updated_at = ? "
            "WHERE device_id = ? AND if_index = ?",
            [
                (sample.oper_status, s.ts, s.device_id, sample.if_index)
                for sample in s.interface_samples
                if sample.oper_status is not None
            ],
        )

    @staticmethod
    def _insert_storage_metrics(conn: sqlite3.Connection, s: DeviceSnapshot) -> None:
        if not s.storages:
            return
        conn.executemany(
            """
            INSERT INTO storages (device_id, storage_key, descr, kind, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id, storage_key) DO UPDATE SET
                descr = excluded.descr,
                kind = excluded.kind,
                updated_at = excluded.updated_at
            """,
            [(s.device_id, st.key, st.descr, st.kind, s.ts) for st in s.storages],
        )
        conn.executemany(
            """
            INSERT INTO storage_metrics (device_id, storage_key, res, ts,
                                         used_bytes, total_bytes)
            VALUES (?, ?, 'raw', ?, ?, ?)
            ON CONFLICT(device_id, storage_key, res, ts) DO NOTHING
            """,
            [(s.device_id, st.key, s.ts, st.used_bytes, st.total_bytes) for st in s.storages],
        )

    async def add_events(self, events: Iterable[Event]) -> None:
        rows = [
            (e.ts, e.device_id, e.if_index, e.severity, e.kind, e.message) for e in events
        ]
        await self.db.executemany(
            "INSERT INTO events (ts, device_id, if_index, severity, kind, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    # -- 読み出し --------------------------------------------------------
    async def list_devices(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM devices ORDER BY name, id")
        return [self._device_row(r) for r in rows]

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM devices WHERE id = ?", (device_id,))
        return self._device_row(row) if row else None

    @staticmethod
    def _device_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["tags"] = json.loads(data.get("tags") or "[]")
        except json.JSONDecodeError:
            data["tags"] = []
        ticks = data.get("uptime_ticks")
        data["uptime_seconds"] = None if ticks is None else ticks / 100.0
        return data

    async def list_interfaces(self, device_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM interfaces WHERE device_id = ? "
            "ORDER BY CAST(if_index AS INTEGER), if_index",
            (device_id,),
        )
        return [dict(r) for r in rows]

    async def list_storages(self, device_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT s.device_id, s.storage_key, s.descr, s.kind,
                   m.used_bytes, m.total_bytes, m.ts
            FROM storages s
            LEFT JOIN storage_metrics m
              ON m.device_id = s.device_id AND m.storage_key = s.storage_key
             AND m.res = 'raw'
             AND m.ts = (SELECT MAX(ts) FROM storage_metrics
                          WHERE device_id = s.device_id
                            AND storage_key = s.storage_key AND res = 'raw')
            WHERE s.device_id = ?
            ORDER BY s.kind, s.descr
            """,
            (device_id,),
        )
        result = []
        for row in rows:
            item = dict(row)
            total = item.get("total_bytes")
            used = item.get("used_bytes")
            item["used_percent"] = (
                round(100.0 * used / total, 2) if total and used is not None else None
            )
            result.append(item)
        return result

    async def interface_history(
        self,
        device_id: str,
        if_index: str,
        start: float,
        end: float,
        res: str | None = None,
    ) -> dict[str, Any]:
        resolution = res or choose_resolution(end - start)
        rows = await self.db.fetchall(
            """
            SELECT ts, in_bps, out_bps, in_bps_max, out_bps_max,
                   in_error_rate, out_error_rate, in_discard_rate, out_discard_rate,
                   oper_status
            FROM interface_metrics
            WHERE device_id = ? AND if_index = ? AND res = ? AND ts >= ? AND ts <= ?
            ORDER BY ts
            """,
            (device_id, if_index, resolution, start, end),
        )
        return {
            "device_id": device_id,
            "if_index": if_index,
            "resolution": resolution,
            "start": start,
            "end": end,
            "points": [dict(r) for r in rows],
        }

    async def device_history(
        self, device_id: str, start: float, end: float, res: str | None = None
    ) -> dict[str, Any]:
        resolution = res or choose_resolution(end - start)
        rows = await self.db.fetchall(
            """
            SELECT ts, reachable, response_ms, cpu_percent, cpu_percent_max,
                   mem_used_bytes, mem_total_bytes
            FROM device_metrics
            WHERE device_id = ? AND res = ? AND ts >= ? AND ts <= ?
            ORDER BY ts
            """,
            (device_id, resolution, start, end),
        )
        return {
            "device_id": device_id,
            "resolution": resolution,
            "start": start,
            "end": end,
            "points": [dict(r) for r in rows],
        }

    async def device_traffic_history(
        self, device_id: str, start: float, end: float, res: str | None = None
    ) -> dict[str, Any]:
        """デバイス配下の全インターフェースを合算したトラフィック履歴。"""
        resolution = res or choose_resolution(end - start)
        rows = await self.db.fetchall(
            """
            SELECT ts,
                   SUM(in_bps)  AS in_bps,
                   SUM(out_bps) AS out_bps
            FROM interface_metrics
            WHERE device_id = ? AND res = ? AND ts >= ? AND ts <= ?
            GROUP BY ts
            ORDER BY ts
            """,
            (device_id, resolution, start, end),
        )
        return {
            "device_id": device_id,
            "resolution": resolution,
            "start": start,
            "end": end,
            "points": [dict(r) for r in rows],
        }

    async def recent_traffic(
        self, start: float, end: float, res: str = RAW
    ) -> dict[str, list[dict[str, Any]]]:
        """全デバイスの合算トラフィックを 1 クエリでまとめて取得する。

        ダッシュボードのスパークラインを台数分のリクエストなしで描くために使う。
        """
        rows = await self.db.fetchall(
            """
            SELECT device_id, ts,
                   SUM(in_bps)  AS in_bps,
                   SUM(out_bps) AS out_bps
            FROM interface_metrics
            WHERE res = ? AND ts >= ? AND ts <= ?
            GROUP BY device_id, ts
            ORDER BY ts
            """,
            (res, start, end),
        )
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["device_id"], []).append(
                {"ts": row["ts"], "in_bps": row["in_bps"], "out_bps": row["out_bps"]}
            )
        return result

    async def list_events(
        self, limit: int = 100, device_id: str | None = None
    ) -> list[dict[str, Any]]:
        if device_id:
            rows = await self.db.fetchall(
                "SELECT * FROM events WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (device_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # -- 集約と削除 ------------------------------------------------------
    async def rollup(self, now: float | None = None) -> None:
        """raw -> 5m -> 1h の順にダウンサンプリングする。"""
        now = now or time.time()
        await self._rollup_resolution(RAW, RES_5M, now)
        await self._rollup_resolution(RES_5M, RES_1H, now)

    async def _rollup_resolution(self, src: str, dst: str, now: float) -> None:
        bucket = BUCKET_SECONDS[dst]
        meta_key = f"rollup_{src}_to_{dst}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (meta_key,))
        since = float(row["value"]) if row else 0.0
        # 直近のバケットはまだ埋まりきっていないので対象から外す
        until = (now // bucket) * bucket
        if until <= since:
            return

        def _run(conn: sqlite3.Connection) -> None:
            params = (bucket, bucket, src, since, until)
            conn.execute(
                f"""
                INSERT INTO interface_metrics
                    (device_id, if_index, res, ts, in_bps, out_bps, in_bps_max,
                     out_bps_max, in_error_rate, out_error_rate, in_discard_rate,
                     out_discard_rate, oper_status)
                SELECT device_id, if_index, '{dst}',
                       CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                       AVG(in_bps), AVG(out_bps), MAX(in_bps_max), MAX(out_bps_max),
                       AVG(in_error_rate), AVG(out_error_rate),
                       AVG(in_discard_rate), AVG(out_discard_rate),
                       MAX(oper_status)
                FROM interface_metrics
                WHERE res = ? AND ts >= ? AND ts < ?
                GROUP BY device_id, if_index, bucket_ts
                ON CONFLICT(device_id, if_index, res, ts) DO UPDATE SET
                    in_bps           = excluded.in_bps,
                    out_bps          = excluded.out_bps,
                    in_bps_max       = excluded.in_bps_max,
                    out_bps_max      = excluded.out_bps_max,
                    in_error_rate    = excluded.in_error_rate,
                    out_error_rate   = excluded.out_error_rate,
                    in_discard_rate  = excluded.in_discard_rate,
                    out_discard_rate = excluded.out_discard_rate,
                    oper_status      = excluded.oper_status
                """,
                params,
            )
            conn.execute(
                f"""
                INSERT INTO device_metrics
                    (device_id, res, ts, reachable, response_ms, cpu_percent,
                     cpu_percent_max, mem_used_bytes, mem_total_bytes, uptime_ticks)
                SELECT device_id, '{dst}',
                       CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                       MIN(reachable), AVG(response_ms), AVG(cpu_percent),
                       MAX(cpu_percent_max), AVG(mem_used_bytes),
                       MAX(mem_total_bytes), MAX(uptime_ticks)
                FROM device_metrics
                WHERE res = ? AND ts >= ? AND ts < ?
                GROUP BY device_id, bucket_ts
                ON CONFLICT(device_id, res, ts) DO UPDATE SET
                    reachable       = excluded.reachable,
                    response_ms     = excluded.response_ms,
                    cpu_percent     = excluded.cpu_percent,
                    cpu_percent_max = excluded.cpu_percent_max,
                    mem_used_bytes  = excluded.mem_used_bytes,
                    mem_total_bytes = excluded.mem_total_bytes,
                    uptime_ticks    = excluded.uptime_ticks
                """,
                params,
            )
            conn.execute(
                f"""
                INSERT INTO storage_metrics
                    (device_id, storage_key, res, ts, used_bytes, total_bytes)
                SELECT device_id, storage_key, '{dst}',
                       CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                       AVG(used_bytes), MAX(total_bytes)
                FROM storage_metrics
                WHERE res = ? AND ts >= ? AND ts < ?
                GROUP BY device_id, storage_key, bucket_ts
                ON CONFLICT(device_id, storage_key, res, ts) DO UPDATE SET
                    used_bytes  = excluded.used_bytes,
                    total_bytes = excluded.total_bytes
                """,
                params,
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (meta_key, str(until)),
            )

        await self.db.run(_run)

    async def prune(self, now: float | None = None) -> None:
        """保持期間を過ぎたデータを削除する。"""
        now = now or time.time()
        cutoffs: Sequence[tuple[str, float]] = (
            (RAW, now - self.config.raw_retention_hours * 3600),
            (RES_5M, now - self.config.rollup_5m_retention_days * 86400),
            (RES_1H, now - self.config.rollup_1h_retention_days * 86400),
        )

        def _run(conn: sqlite3.Connection) -> None:
            for res, cutoff in cutoffs:
                for table in ("interface_metrics", "device_metrics", "storage_metrics"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE res = ? AND ts < ?", (res, cutoff)
                    )
            conn.execute(
                "DELETE FROM events WHERE ts < ?",
                (now - self.config.rollup_1h_retention_days * 86400,),
            )

        await self.db.run(_run)
