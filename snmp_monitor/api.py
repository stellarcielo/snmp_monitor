"""FastAPI による REST / WebSocket バックエンド。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import AppConfig, DeviceConfig
from .models import DeviceSnapshot, InterfaceInfo
from .poller import ClientFactory, Poller
from .portmap import PANEL_CATEGORIES, build_port_map
from .simulator import SimulatedSnmpClient
from .snmp.client import SnmpClient
from .snmp.oids import status_label
from .storage.db import Database
from .storage.timeseries import TimeSeriesStore, choose_resolution

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

#: 区分がまだ決まっていないインターフェースの扱い
PHYSICAL_FALLBACK = "physical"

#: 更新したはずの画面が古いまま表示されないよう、毎回サーバに確認させる。
#: ETag での再検証は残るので、変更がなければ 304 で済む。
NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}


class NoCacheStaticFiles(StaticFiles):
    """ブラウザに必ず更新確認をさせる静的ファイル配信。"""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

#: UI から指定できる表示期間
RANGE_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}


def parse_range(value: str) -> int:
    if value not in RANGE_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"range は {', '.join(RANGE_SECONDS)} のいずれかを指定してください",
        )
    return RANGE_SECONDS[value]


class ConnectionManager:
    """WebSocket 接続をまとめて扱う。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections)
        if not targets:
            return
        payload = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_text(payload)
            except Exception:  # noqa: BLE001 - 切断済みの接続は取り除くだけ
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections.discard(websocket)

    def __len__(self) -> int:
        return len(self._connections)


def create_app(config: AppConfig, client_factory: ClientFactory | None = None) -> FastAPI:
    """設定から FastAPI アプリを組み立てる。

    ``client_factory`` は SNMP クライアントの差し替え口。通常は省略し、
    デモモードならシミュレータ、そうでなければ実機用の :class:`SnmpClient` を使う。
    """

    database = Database(config.storage.path)
    store = TimeSeriesStore(database, config.storage)
    factory = client_factory or (SimulatedSnmpClient if config.demo_mode else SnmpClient)
    poller = Poller(config, store, client_factory=factory)
    manager = ConnectionManager()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.connect()
        poller.subscribe(manager.broadcast)
        await poller.start()
        try:
            yield
        finally:
            poller.unsubscribe(manager.broadcast)
            await poller.stop()
            await database.close()

    app = FastAPI(title="SNMP Monitor", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.poller = poller
    app.state.manager = manager

    # -- ヘルパ ----------------------------------------------------------
    def device_config(device_id: str) -> DeviceConfig:
        device = config.device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=f"未知のデバイスです: {device_id}")
        return device

    def merge_device(
        device: DeviceConfig, row: dict[str, Any] | None, snapshot: DeviceSnapshot | None
    ) -> dict[str, Any]:
        """設定・DB の最終状態・メモリ上の最新値をまとめて 1 件の JSON にする。"""
        row = row or {}
        data: dict[str, Any] = {
            "id": device.id,
            "name": device.display_name,
            "host": device.host,
            "port": device.port,
            "tags": device.tags,
            "enabled": device.enabled,
            "snmp_version": device.snmp.version,
            "status": row.get("status", "unknown"),
            "error": row.get("last_error"),
            "sys_name": row.get("sys_name"),
            "sys_descr": row.get("sys_descr"),
            "sys_location": row.get("sys_location"),
            "sys_contact": row.get("sys_contact"),
            "uptime_seconds": row.get("uptime_seconds"),
            "last_poll_at": row.get("last_poll_at"),
            "last_up_at": row.get("last_up_at"),
            "response_ms": None,
            "cpu_percent": None,
            "mem_used_bytes": None,
            "mem_total_bytes": None,
            "mem_percent": None,
            "in_bps": None,
            "out_bps": None,
        }
        if snapshot is not None:
            data.update(
                {
                    "status": "up" if snapshot.reachable else "down",
                    "error": snapshot.error,
                    "sys_name": snapshot.sys_name or data["sys_name"],
                    "sys_descr": snapshot.sys_descr or data["sys_descr"],
                    "sys_location": snapshot.sys_location or data["sys_location"],
                    "uptime_seconds": snapshot.uptime_seconds,
                    "last_poll_at": snapshot.ts,
                    "response_ms": snapshot.response_ms,
                    "cpu_percent": snapshot.cpu_percent,
                    "mem_used_bytes": snapshot.mem_used_bytes,
                    "mem_total_bytes": snapshot.mem_total_bytes,
                    "mem_percent": snapshot.mem_percent,
                    "in_bps": _sum_rate(snapshot, "in_bps"),
                    "out_bps": _sum_rate(snapshot, "out_bps"),
                }
            )
        return data

    def port_counts(device_id: str, snapshot: DeviceSnapshot | None) -> dict[str, int]:
        """物理ポート (SFP を含む) だけを数える。"""
        if snapshot is None:
            return {"total": 0, "up": 0}
        categories = {i.if_index: i.category for i in poller.interfaces(device_id)}
        physical = [
            s
            for s in snapshot.interface_samples
            # 区分がまだ決まっていない初回は、ひとまず物理として数える
            if categories.get(s.if_index, PHYSICAL_FALLBACK) in PANEL_CATEGORIES
        ]
        return {
            "total": len(physical),
            "up": sum(1 for s in physical if s.oper_status == 1),
        }

    # -- API -------------------------------------------------------------
    @app.get("/api/summary")
    async def get_summary() -> dict[str, Any]:
        rows = {r["id"]: r for r in await store.list_devices()}
        devices = []
        for device in config.devices:
            snapshot = poller.snapshot(device.id)
            item = merge_device(device, rows.get(device.id), snapshot)
            item["ports"] = port_counts(device.id, snapshot)
            devices.append(item)
        up = sum(1 for d in devices if d["status"] == "up")
        return {
            "generated_at": time.time(),
            "demo_mode": config.demo_mode,
            "poll_interval": config.polling.interval_seconds,
            "devices_total": len(devices),
            "devices_up": up,
            "devices_down": len(devices) - up,
            "total_in_bps": sum(d["in_bps"] or 0 for d in devices),
            "total_out_bps": sum(d["out_bps"] or 0 for d in devices),
            "ports_total": sum(d["ports"]["total"] for d in devices),
            "ports_up": sum(d["ports"]["up"] for d in devices),
            "devices": devices,
        }

    @app.get("/api/devices")
    async def list_devices() -> list[dict[str, Any]]:
        summary = await get_summary()
        return summary["devices"]

    @app.get("/api/devices/{device_id}")
    async def get_device(device_id: str) -> dict[str, Any]:
        device = device_config(device_id)
        snapshot = poller.snapshot(device_id)
        data = merge_device(device, await store.get_device(device_id), snapshot)
        data["ports"] = port_counts(device_id, snapshot)
        data["storages"] = (
            [s.to_dict() for s in snapshot.storages]
            if snapshot and snapshot.storages
            else await store.list_storages(device_id)
        )
        interfaces = await build_interfaces(device_id, snapshot)
        data["interfaces"] = interfaces
        data["port_map"] = build_or_reuse_port_map(device, interfaces).to_dict()
        return data

    def build_or_reuse_port_map(device: DeviceConfig, interfaces: list[dict[str, Any]]):
        """ポーリング済みならその結果を使い、未ポーリングなら DB の情報から作る。"""
        cached = poller.port_map(device.id)
        if cached is not None:
            return cached
        infos = [
            InterfaceInfo(
                if_index=row["if_index"],
                name=row.get("name"),
                descr=row.get("descr"),
                alias=row.get("alias"),
                if_type=row.get("if_type"),
                speed_bps=row.get("speed_bps"),
                mtu=row.get("mtu"),
                mac=row.get("mac"),
                admin_status=row.get("admin_status"),
            )
            for row in interfaces
        ]
        return build_port_map(infos, device.port_layout)

    async def build_interfaces(
        device_id: str, snapshot: DeviceSnapshot | None
    ) -> list[dict[str, Any]]:
        """構成情報 (DB) と最新のレート (メモリ) を結合する。"""
        stored = {row["if_index"]: dict(row) for row in await store.list_interfaces(device_id)}
        for info in poller.interfaces(device_id):
            stored.setdefault(info.if_index, {}).update(
                {
                    "device_id": device_id,
                    "if_index": info.if_index,
                    "name": info.name,
                    "descr": info.descr,
                    "alias": info.alias,
                    "if_type": info.if_type,
                    "speed_bps": info.speed_bps,
                    "mtu": info.mtu,
                    "mac": info.mac,
                    "admin_status": info.admin_status,
                }
            )
        samples = {s.if_index: s for s in (snapshot.interface_samples if snapshot else [])}
        categories = {i.if_index: i.category for i in poller.interfaces(device_id)}
        result = []
        for if_index, row in stored.items():
            row["category"] = categories.get(if_index)
            sample = samples.get(if_index)
            if sample is not None:
                row["oper_status"] = sample.oper_status
                row["in_bps"] = sample.in_bps
                row["out_bps"] = sample.out_bps
                row["in_error_rate"] = sample.in_error_rate
                row["out_error_rate"] = sample.out_error_rate
                row["in_discard_rate"] = sample.in_discard_rate
                row["out_discard_rate"] = sample.out_discard_rate
            row.setdefault("in_bps", None)
            row.setdefault("out_bps", None)
            row["label"] = row.get("name") or row.get("descr") or f"if{if_index}"
            row["oper_status_label"] = status_label(row.get("oper_status"))
            row["admin_status_label"] = status_label(row.get("admin_status"))
            result.append(row)
        result.sort(key=lambda r: _index_sort_key(r["if_index"]))
        return result

    @app.get("/api/devices/{device_id}/interfaces")
    async def list_interfaces(device_id: str) -> list[dict[str, Any]]:
        device_config(device_id)
        return await build_interfaces(device_id, poller.snapshot(device_id))

    @app.get("/api/devices/{device_id}/portmap")
    async def port_map(device_id: str) -> dict[str, Any]:
        """区分と物理配置の推定結果を返す。"""
        device = device_config(device_id)
        interfaces = await build_interfaces(device_id, poller.snapshot(device_id))
        return build_or_reuse_port_map(device, interfaces).to_dict()

    @app.get("/api/devices/{device_id}/history")
    async def device_history(
        device_id: str, range: str = Query(default="1h")
    ) -> dict[str, Any]:
        device_config(device_id)
        span = parse_range(range)
        end = time.time()
        start = end - span
        resolution = choose_resolution(span)
        metrics = await store.device_history(device_id, start, end, resolution)
        traffic = await store.device_traffic_history(device_id, start, end, resolution)
        return {
            "device_id": device_id,
            "range": range,
            "resolution": resolution,
            "start": start,
            "end": end,
            "metrics": metrics["points"],
            "traffic": traffic["points"],
        }

    @app.get("/api/devices/{device_id}/interfaces/{if_index}/history")
    async def interface_history(
        device_id: str, if_index: str, range: str = Query(default="1h")
    ) -> dict[str, Any]:
        device_config(device_id)
        span = parse_range(range)
        end = time.time()
        result = await store.interface_history(device_id, if_index, end - span, end)
        result["range"] = range
        return result

    @app.get("/api/sparklines")
    async def sparklines(range: str = Query(default="15m")) -> dict[str, Any]:
        """ダッシュボードのカードに出す短い時系列をまとめて返す。"""
        span = parse_range(range)
        end = time.time()
        data = await store.recent_traffic(end - span, end)
        return {"range": range, "start": end - span, "end": end, "devices": data}

    @app.get("/api/events")
    async def list_events(
        limit: int = Query(default=100, ge=1, le=1000),
        device_id: str | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return await store.list_events(limit=limit, device_id=device_id)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            # 接続直後に現在の状態を 1 度流しておく
            await websocket.send_text(
                json.dumps(
                    {"type": "summary", "data": await get_summary()},
                    ensure_ascii=False,
                    default=str,
                )
            )
            while True:
                # クライアントからの ping 以外は特に扱わない
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("WebSocket でエラーが発生しました", exc_info=True)
        finally:
            await manager.disconnect(websocket)

    # -- 静的ファイル ----------------------------------------------------
    if WEB_DIR.exists():
        app.mount("/static", NoCacheStaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE_HEADERS)
    else:  # pragma: no cover - 開発時のみ

        @app.get("/")
        async def index_missing() -> JSONResponse:
            return JSONResponse({"detail": "web ディレクトリが見つかりません"}, status_code=500)

    return app


def _sum_rate(snapshot: DeviceSnapshot, attribute: str) -> float | None:
    values = [
        getattr(s, attribute)
        for s in snapshot.interface_samples
        if getattr(s, attribute) is not None
    ]
    return sum(values) if values else None


def _index_sort_key(if_index: str) -> tuple[int, str]:
    try:
        return (int(if_index), "")
    except ValueError:
        return (10**9, if_index)
