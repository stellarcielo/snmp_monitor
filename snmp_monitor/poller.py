"""SNMP の定期ポーリングとイベント検出。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, DeviceConfig
from .metrics import RateCalculator, max_plausible_rate, octets_to_bps, speed_to_bps
from .models import DeviceSnapshot, Event, InterfaceInfo, InterfaceSample, StorageInfo
from .portmap import PortMap, build_port_map
from .snmp import oids
from .snmp.client import SnmpClient, SnmpError, decode_str, format_mac
from .storage.timeseries import TimeSeriesStore

logger = logging.getLogger(__name__)

ClientFactory = Callable[[DeviceConfig], Any]
Listener = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class DeviceState:
    """ポーリング間で引き継ぐデバイスごとの状態。"""

    interfaces: dict[str, InterfaceInfo] = field(default_factory=dict)
    port_map: PortMap | None = None
    poll_count: int = 0
    last_uptime_ticks: int | None = None
    last_oper_status: dict[str, int | None] = field(default_factory=dict)
    reachable: bool | None = None
    snapshot: DeviceSnapshot | None = None


class Poller:
    """設定された全デバイスを定期的にポーリングする。"""

    def __init__(
        self,
        config: AppConfig,
        store: TimeSeriesStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._client_factory = client_factory or SnmpClient
        self._rates = RateCalculator()
        self._states: dict[str, DeviceState] = {}
        self._clients: dict[str, Any] = {}
        self._listeners: set[Listener] = set()
        self._semaphore = asyncio.Semaphore(config.polling.max_concurrent_devices)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    # -- ライフサイクル --------------------------------------------------
    async def start(self) -> None:
        for device in self.config.enabled_devices:
            await self.store.register_device(device)
            self._states[device.id] = DeviceState()
            self._tasks.append(
                asyncio.create_task(self._device_loop(device), name=f"poll:{device.id}")
            )
        self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="maintenance"))
        logger.info("ポーリングを開始しました (%d 台)", len(self.config.enabled_devices))

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        logger.info("ポーリングを停止しました")

    # -- 購読 ------------------------------------------------------------
    def subscribe(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                await listener(message)
            except Exception:  # noqa: BLE001 - 1 つの購読者の失敗で全体を止めない
                logger.debug("購読者への通知に失敗しました", exc_info=True)

    # -- 現在値の参照 ----------------------------------------------------
    def snapshot(self, device_id: str) -> DeviceSnapshot | None:
        state = self._states.get(device_id)
        return state.snapshot if state else None

    def snapshots(self) -> dict[str, DeviceSnapshot]:
        return {
            device_id: state.snapshot
            for device_id, state in self._states.items()
            if state.snapshot is not None
        }

    def interfaces(self, device_id: str) -> list[InterfaceInfo]:
        state = self._states.get(device_id)
        return list(state.interfaces.values()) if state else []

    def port_map(self, device_id: str) -> PortMap | None:
        """最後に構成を取得したときのポートマップ。"""
        state = self._states.get(device_id)
        return state.port_map if state else None

    # -- ループ ----------------------------------------------------------
    async def _device_loop(self, device: DeviceConfig) -> None:
        interval = self.config.polling.interval_seconds
        # 全デバイスが同時に走らないよう開始をばらす
        await asyncio.sleep(random.uniform(0, min(interval, 5.0)))
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                async with self._semaphore:
                    await self.poll_once(device)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ループは決して落とさない
                logger.exception("%s のポーリングで予期しないエラーが発生しました", device.id)
            elapsed = time.monotonic() - started
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=max(1.0, interval - elapsed)
                )

    async def _maintenance_loop(self) -> None:
        interval = self.store.config.rollup_interval_seconds
        while not self._stopping.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            if self._stopping.is_set():
                return
            try:
                await self.store.rollup()
                await self.store.prune()
            except Exception:  # noqa: BLE001
                logger.exception("集約処理に失敗しました")

    # -- 1 回分のポーリング ----------------------------------------------
    async def poll_once(self, device: DeviceConfig, ts: float | None = None) -> DeviceSnapshot:
        """デバイスを 1 回ポーリングする。

        ``ts`` は記録に使う時刻で、レート計算の分母にもなる。
        通常は省略して現在時刻を使い、テストでのみ明示的に与える。
        """
        state = self._states.setdefault(device.id, DeviceState())
        state.poll_count += 1
        ts = time.time() if ts is None else ts
        started = time.monotonic()

        try:
            client = self._get_client(device)
            snapshot = await self._collect(client, device, state, ts)
            snapshot.response_ms = round((time.monotonic() - started) * 1000, 2)
        except SnmpError as exc:
            snapshot = DeviceSnapshot(
                device_id=device.id, ts=ts, reachable=False, error=str(exc)
            )
            # 応答が無いデバイスのカウンタ基準値は破棄しておく
            self._rates.forget_prefix(f"{device.id}:")
            await self._drop_client(device.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s の収集に失敗しました", device.id)
            snapshot = DeviceSnapshot(
                device_id=device.id, ts=ts, reachable=False, error=str(exc)
            )
            await self._drop_client(device.id)

        events = self._detect_events(device, state, snapshot)
        state.snapshot = snapshot
        state.reachable = snapshot.reachable

        await self.store.save_snapshot(snapshot)
        if events:
            await self.store.add_events(events)

        await self._broadcast(
            {
                "type": "snapshot",
                "device": self._public_snapshot(device, snapshot, state),
                "events": [e.to_dict() for e in events],
            }
        )
        return snapshot

    def _public_snapshot(
        self, device: DeviceConfig, snapshot: DeviceSnapshot, state: DeviceState
    ) -> dict[str, Any]:
        data = snapshot.to_dict()
        data["name"] = device.display_name
        data["host"] = device.host
        data["tags"] = device.tags
        data["status"] = "up" if snapshot.reachable else "down"
        if data["interfaces"] is None:
            data["interfaces"] = [i.to_dict() for i in state.interfaces.values()]
        return data

    def _get_client(self, device: DeviceConfig) -> Any:
        client = self._clients.get(device.id)
        if client is None:
            client = self._client_factory(device)
            self._clients[device.id] = client
        return client

    async def _drop_client(self, device_id: str) -> None:
        client = self._clients.pop(device_id, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    # -- 収集処理 --------------------------------------------------------
    async def _collect(
        self, client: Any, device: DeviceConfig, state: DeviceState, ts: float
    ) -> DeviceSnapshot:
        system = await client.get(oids.SYSTEM_OIDS)
        snapshot = DeviceSnapshot(
            device_id=device.id,
            ts=ts,
            reachable=True,
            sys_name=decode_str(system.get("sys_name")),
            sys_descr=decode_str(system.get("sys_descr")),
            sys_object_id=(
                str(system["sys_object_id"]) if system.get("sys_object_id") else None
            ),
            sys_location=decode_str(system.get("sys_location")),
            sys_contact=decode_str(system.get("sys_contact")),
            uptime_ticks=system.get("uptime_ticks"),
        )

        # 機器が再起動した場合はカウンタが 0 に戻るため基準値を捨てる
        if self._detect_restart(state, snapshot.uptime_ticks):
            logger.info("%s の再起動を検出しました", device.id)
            self._rates.forget_prefix(f"{device.id}:")
        state.last_uptime_ticks = snapshot.uptime_ticks

        polls = state.poll_count
        need_inventory = (
            not state.interfaces
            or polls % self.config.polling.inventory_every_n_polls == 1
            or self.config.polling.inventory_every_n_polls == 1
        )
        if need_inventory:
            state.interfaces, state.port_map = await self._fetch_inventory(client, device)
            snapshot.interfaces = list(state.interfaces.values())

        snapshot.interface_samples = await self._fetch_interface_samples(
            client, device, state, ts
        )

        if polls % self.config.polling.resource_every_n_polls == 0 or polls == 1:
            cpu, storages = await self._fetch_resources(client)
            snapshot.cpu_percent = cpu
            snapshot.storages = storages
            ram = next((s for s in storages if s.kind == "ram"), None)
            if ram:
                snapshot.mem_used_bytes = ram.used_bytes
                snapshot.mem_total_bytes = ram.total_bytes
        return snapshot

    @staticmethod
    def _detect_restart(state: DeviceState, uptime_ticks: int | None) -> bool:
        if uptime_ticks is None or state.last_uptime_ticks is None:
            return False
        return uptime_ticks < state.last_uptime_ticks

    async def _fetch_inventory(
        self, client: Any, device: DeviceConfig
    ) -> tuple[dict[str, InterfaceInfo], PortMap]:
        base = await client.get_table(oids.INTERFACE_INVENTORY_COLUMNS)
        try:
            extended = await client.get_table(oids.INTERFACE_INVENTORY_COLUMNS_X)
        except SnmpError:
            # ifXTable 非対応の機器では ifTable のみで構成する
            extended = {}

        include = re.compile(device.interface_include) if device.interface_include else None
        exclude = re.compile(device.interface_exclude) if device.interface_exclude else None

        result: dict[str, InterfaceInfo] = {}
        for if_index, row in base.items():
            ext = extended.get(if_index, {})
            name = decode_str(ext.get("name"))
            descr = decode_str(row.get("descr"))
            label = name or descr or f"if{if_index}"
            if include and not include.search(label):
                continue
            if exclude and exclude.search(label):
                continue
            result[if_index] = InterfaceInfo(
                if_index=if_index,
                name=name,
                descr=descr,
                alias=decode_str(ext.get("alias")),
                if_type=row.get("if_type"),
                speed_bps=speed_to_bps(row.get("speed"), ext.get("high_speed")),
                mtu=row.get("mtu"),
                mac=format_mac(row.get("phys_address")),
                admin_status=row.get("admin_status"),
                last_change=row.get("last_change"),
            )

        # 区分と物理配置はここで一度だけ決めて、以降のポーリングでは使い回す
        port_map = build_port_map(list(result.values()), device.port_layout)
        for info in result.values():
            info.category = port_map.categories.get(info.if_index)
        return result, port_map

    async def _fetch_interface_samples(
        self, client: Any, device: DeviceConfig, state: DeviceState, ts: float
    ) -> list[InterfaceSample]:
        counters = await client.get_table(oids.INTERFACE_COUNTER_COLUMNS)
        try:
            counters64 = await client.get_table(oids.INTERFACE_COUNTER_COLUMNS_X)
        except SnmpError:
            counters64 = {}

        samples: list[InterfaceSample] = []
        for if_index, row in counters.items():
            if state.interfaces and if_index not in state.interfaces:
                continue
            info = state.interfaces.get(if_index)
            wide = counters64.get(if_index, {})
            in_octets = wide.get("in_octets_64")
            out_octets = wide.get("out_octets_64")
            bits = 64
            if in_octets is None and out_octets is None:
                in_octets = row.get("in_octets_32")
                out_octets = row.get("out_octets_32")
                bits = 32

            limit = max_plausible_rate(info.speed_bps if info else None)
            prefix = f"{device.id}:{if_index}:"
            sample = InterfaceSample(
                if_index=if_index,
                oper_status=row.get("oper_status"),
                in_bps=octets_to_bps(
                    self._rates.update(
                        f"{prefix}in", in_octets, ts, bits=bits, max_rate=limit
                    )
                ),
                out_bps=octets_to_bps(
                    self._rates.update(
                        f"{prefix}out", out_octets, ts, bits=bits, max_rate=limit
                    )
                ),
                in_error_rate=self._rates.update(
                    f"{prefix}inerr", row.get("in_errors"), ts, bits=32
                ),
                out_error_rate=self._rates.update(
                    f"{prefix}outerr", row.get("out_errors"), ts, bits=32
                ),
                in_discard_rate=self._rates.update(
                    f"{prefix}indisc", row.get("in_discards"), ts, bits=32
                ),
                out_discard_rate=self._rates.update(
                    f"{prefix}outdisc", row.get("out_discards"), ts, bits=32
                ),
                in_octets=in_octets,
                out_octets=out_octets,
            )
            samples.append(sample)
        return samples

    async def _fetch_resources(self, client: Any) -> tuple[float | None, list[StorageInfo]]:
        cpu: float | None = None
        storages: list[StorageInfo] = []

        # 1) HOST-RESOURCES-MIB
        try:
            loads = await client.walk(oids.HR_PROCESSOR_LOAD)
            values = [float(v) for v in loads.values() if isinstance(v, int)]
            if values:
                cpu = round(sum(values) / len(values), 2)
        except SnmpError:
            logger.debug("hrProcessorLoad を取得できませんでした", exc_info=True)

        try:
            storages = self._parse_hr_storage(await client.get_table(oids.HR_STORAGE_COLUMNS))
        except SnmpError:
            logger.debug("hrStorageTable を取得できませんでした", exc_info=True)

        # 2) 取得できなければ UCD-SNMP-MIB にフォールバック
        if cpu is None or not storages:
            ucd_cpu, ucd_storages = await self._fetch_ucd_resources(client)
            cpu = cpu if cpu is not None else ucd_cpu
            if not storages:
                storages = ucd_storages
        return cpu, storages

    @staticmethod
    def _parse_hr_storage(table: dict[str, dict[str, Any]]) -> list[StorageInfo]:
        kinds = {
            oids.HR_STORAGE_TYPE_RAM: "ram",
            oids.HR_STORAGE_TYPE_VIRTUAL_MEMORY: "swap",
            oids.HR_STORAGE_TYPE_FIXED_DISK: "disk",
        }
        result: list[StorageInfo] = []
        for index, row in table.items():
            storage_type = row.get("storage_type")
            kind = kinds.get(str(storage_type) if storage_type else "", None)
            if kind is None:
                continue
            units = row.get("allocation_units") or 1
            size = row.get("size")
            used = row.get("used")
            if not isinstance(size, int) or not isinstance(used, int):
                continue
            descr = decode_str(row.get("descr"))
            # ネットワーク機器では tmpfs 等も FixedDisk として現れるため件数を抑える
            result.append(
                StorageInfo(
                    key=f"hr:{index}",
                    descr=descr,
                    kind=kind,
                    used_bytes=used * units,
                    total_bytes=size * units,
                )
            )
        return result

    async def _fetch_ucd_resources(
        self, client: Any
    ) -> tuple[float | None, list[StorageInfo]]:
        cpu: float | None = None
        storages: list[StorageInfo] = []
        try:
            values = await client.get(oids.UCD_CPU_MEM_OIDS)
        except SnmpError:
            return None, []

        idle = values.get("cpu_idle")
        if isinstance(idle, int):
            cpu = round(max(0.0, 100.0 - float(idle)), 2)

        total_kb = values.get("mem_total_kb")
        avail_kb = values.get("mem_avail_kb")
        if isinstance(total_kb, int) and isinstance(avail_kb, int) and total_kb > 0:
            buffer_kb = values.get("mem_buffer_kb") or 0
            cached_kb = values.get("mem_cached_kb") or 0
            used_kb = max(0, total_kb - avail_kb - int(buffer_kb) - int(cached_kb))
            storages.append(
                StorageInfo(
                    key="ucd:mem",
                    descr="Physical memory",
                    kind="ram",
                    used_bytes=used_kb * 1024,
                    total_bytes=total_kb * 1024,
                )
            )

        try:
            disks = await client.get_table(oids.UCD_DISK_COLUMNS)
        except SnmpError:
            disks = {}
        for index, row in disks.items():
            total = row.get("total_kb")
            used = row.get("used_kb")
            if not isinstance(total, int) or not isinstance(used, int) or total <= 0:
                continue
            storages.append(
                StorageInfo(
                    key=f"ucd:disk:{index}",
                    descr=decode_str(row.get("path")),
                    kind="disk",
                    used_bytes=used * 1024,
                    total_bytes=total * 1024,
                )
            )
        return cpu, storages

    # -- イベント検出 ----------------------------------------------------
    def _detect_events(
        self, device: DeviceConfig, state: DeviceState, snapshot: DeviceSnapshot
    ) -> list[Event]:
        events: list[Event] = []
        name = device.display_name

        if state.reachable is not None and state.reachable != snapshot.reachable:
            if snapshot.reachable:
                events.append(
                    Event(
                        ts=snapshot.ts,
                        device_id=device.id,
                        kind="device_up",
                        severity="info",
                        message=f"{name} が応答を再開しました",
                    )
                )
            else:
                events.append(
                    Event(
                        ts=snapshot.ts,
                        device_id=device.id,
                        kind="device_down",
                        severity="critical",
                        message=f"{name} が応答しません ({snapshot.error or '原因不明'})",
                    )
                )

        if snapshot.reachable and self._detect_restart_event(state, snapshot):
            events.append(
                Event(
                    ts=snapshot.ts,
                    device_id=device.id,
                    kind="device_restart",
                    severity="warning",
                    message=f"{name} の再起動を検出しました",
                )
            )

        for sample in snapshot.interface_samples:
            previous = state.last_oper_status.get(sample.if_index)
            current = sample.oper_status
            state.last_oper_status[sample.if_index] = current
            if previous is None or previous == current:
                continue
            info = state.interfaces.get(sample.if_index)
            label = info.label if info else f"if{sample.if_index}"
            if current == 1:
                events.append(
                    Event(
                        ts=snapshot.ts,
                        device_id=device.id,
                        if_index=sample.if_index,
                        kind="link_up",
                        severity="info",
                        message=f"{name} / {label} がリンクアップしました",
                    )
                )
            elif previous == 1:
                events.append(
                    Event(
                        ts=snapshot.ts,
                        device_id=device.id,
                        if_index=sample.if_index,
                        kind="link_down",
                        severity="warning",
                        message=f"{name} / {label} がリンクダウンしました",
                    )
                )
        return events

    @staticmethod
    def _detect_restart_event(state: DeviceState, snapshot: DeviceSnapshot) -> bool:
        previous = state.snapshot
        if previous is None or previous.uptime_ticks is None:
            return False
        if snapshot.uptime_ticks is None:
            return False
        return snapshot.uptime_ticks < previous.uptime_ticks
