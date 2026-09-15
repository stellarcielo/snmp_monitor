"""ポーラーの収集処理とイベント検出のテスト。"""

from __future__ import annotations

from typing import Any

from snmp_monitor.config import AppConfig, DeviceConfig, PollingConfig
from snmp_monitor.poller import Poller
from snmp_monitor.simulator import SimulatedSnmpClient, demo_config
from snmp_monitor.snmp import oids
from snmp_monitor.snmp.client import SnmpTimeoutError


class FakeDevice:
    """テストから値を自由に動かせる仮想エージェント。"""

    def __init__(self) -> None:
        self.reachable = True
        self.uptime_ticks = 1_000_000
        self.now = 0.0
        self.ports = {
            "1": {"oper": 1, "in": 0, "out": 0, "errors": 0},
            "2": {"oper": 1, "in": 0, "out": 0, "errors": 0},
        }
        self.supports_ifx = True
        self.supports_hr = True

    def advance(self, seconds: float, octets_per_second: int = 1_000_000) -> None:
        self.now += seconds
        self.uptime_ticks += int(seconds * 100)
        for port in self.ports.values():
            if port["oper"] == 1:
                port["in"] += int(octets_per_second * seconds)
                port["out"] += int(octets_per_second * seconds / 2)


class FakeClient:
    def __init__(self, device: DeviceConfig, state: FakeDevice) -> None:
        self.device = device
        self.state = state

    async def close(self) -> None:
        return None

    def _check(self) -> None:
        if not self.state.reachable:
            raise SnmpTimeoutError("No SNMP response received before timeout")

    async def get(self, oid_map: Any) -> dict[str, Any]:
        self._check()
        values = {
            "sys_descr": b"Fake device",
            "sys_object_id": "1.3.6.1.4.1.1.1",
            "uptime_ticks": self.state.uptime_ticks,
            "sys_contact": b"admin",
            "sys_name": b"fake01",
            "sys_location": b"lab",
        }
        return {name: values.get(name) for name in oid_map}

    async def get_table(self, columns: Any, **_: Any) -> dict[str, dict[str, Any]]:
        self._check()
        keys = set(columns)
        if keys == set(oids.INTERFACE_INVENTORY_COLUMNS):
            return {
                index: {
                    "descr": f"eth{index}".encode(),
                    "if_type": 6,
                    "mtu": 1500,
                    "speed": 1_000_000_000,
                    "phys_address": bytes([0, 0, 0, 0, 0, int(index)]),
                    "admin_status": 1,
                    "last_change": 0,
                }
                for index in self.state.ports
            }
        if keys == set(oids.INTERFACE_INVENTORY_COLUMNS_X):
            if not self.state.supports_ifx:
                raise SnmpTimeoutError("ifXTable is not supported")
            return {
                index: {"name": f"Port {index}".encode(), "high_speed": 1000, "alias": b""}
                for index in self.state.ports
            }
        if keys == set(oids.INTERFACE_COUNTER_COLUMNS):
            return {
                index: {
                    "oper_status": port["oper"],
                    "in_octets_32": port["in"] % (2**32),
                    "out_octets_32": port["out"] % (2**32),
                    "in_errors": port["errors"],
                    "out_errors": 0,
                    "in_discards": 0,
                    "out_discards": 0,
                }
                for index, port in self.state.ports.items()
            }
        if keys == set(oids.INTERFACE_COUNTER_COLUMNS_X):
            if not self.state.supports_ifx:
                raise SnmpTimeoutError("ifXTable is not supported")
            return {
                index: {"in_octets_64": port["in"], "out_octets_64": port["out"]}
                for index, port in self.state.ports.items()
            }
        if keys == set(oids.HR_STORAGE_COLUMNS):
            if not self.state.supports_hr:
                raise SnmpTimeoutError("HOST-RESOURCES-MIB is not supported")
            return {
                "1": {
                    "storage_type": oids.HR_STORAGE_TYPE_RAM,
                    "descr": b"Physical memory",
                    "allocation_units": 1024,
                    "size": 1024,
                    "used": 256,
                }
            }
        if keys == set(oids.UCD_DISK_COLUMNS):
            return {}
        return {}

    async def walk(self, base_oid: str, **_: Any) -> dict[str, Any]:
        self._check()
        if base_oid == oids.HR_PROCESSOR_LOAD and self.state.supports_hr:
            return {"1": 30, "2": 50}
        return {}


def build_poller(store, state: FakeDevice, **polling: Any):
    device = DeviceConfig(id="fake1", name="Fake", host="127.0.0.1")
    config = AppConfig(
        devices=[device],
        polling=PollingConfig(interval_seconds=5.0, **polling),
    )
    poller = Poller(config, store, client_factory=lambda d: FakeClient(d, state))
    return poller, device


async def test_first_poll_has_no_rates(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    snapshot = await poller.poll_once(device)

    assert snapshot.reachable is True
    assert snapshot.sys_name == "fake01"
    # 1 回目は前回値がないのでレートは出ない
    assert all(s.in_bps is None for s in snapshot.interface_samples)


async def test_second_poll_computes_bps(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    await poller.poll_once(device, ts=1000.0)
    state.advance(10, octets_per_second=1_000_000)
    snapshot = await poller.poll_once(device, ts=1010.0)

    sample = next(s for s in snapshot.interface_samples if s.if_index == "1")
    # 1,000,000 オクテット/秒 = 8 Mbps
    assert sample.in_bps == 8_000_000
    assert sample.out_bps == 4_000_000


async def test_cpu_and_memory_are_collected(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    snapshot = await poller.poll_once(device)
    assert snapshot.cpu_percent == 40.0  # (30 + 50) / 2
    assert snapshot.mem_total_bytes == 1024 * 1024
    assert snapshot.mem_percent == 25.0


async def test_falls_back_when_ifxtable_is_unsupported(store):
    state = FakeDevice()
    state.supports_ifx = False
    poller, device = build_poller(store, state)
    await poller.poll_once(device, ts=1000.0)
    state.advance(10)
    snapshot = await poller.poll_once(device, ts=1010.0)

    # 32bit カウンタでもレートが求まる
    sample = next(s for s in snapshot.interface_samples if s.if_index == "1")
    assert sample.in_bps == 8_000_000
    # ifXTable が無い場合は ifDescr が名前になる
    info = next(i for i in poller.interfaces("fake1") if i.if_index == "1")
    assert info.descr == "eth1"


async def test_device_down_and_up_events(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    await poller.poll_once(device)

    state.reachable = False
    snapshot = await poller.poll_once(device)
    assert snapshot.reachable is False
    events = await store.list_events()
    assert events[0]["kind"] == "device_down"
    assert events[0]["severity"] == "critical"

    state.reachable = True
    await poller.poll_once(device)
    events = await store.list_events()
    assert events[0]["kind"] == "device_up"


async def test_link_down_and_up_events(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    await poller.poll_once(device)

    state.ports["2"]["oper"] = 2
    await poller.poll_once(device)
    events = await store.list_events()
    assert events[0]["kind"] == "link_down"
    assert events[0]["if_index"] == "2"

    state.ports["2"]["oper"] = 1
    await poller.poll_once(device)
    events = await store.list_events()
    assert events[0]["kind"] == "link_up"


async def test_restart_resets_counters_and_raises_event(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    await poller.poll_once(device, ts=1000.0)
    state.advance(10)
    await poller.poll_once(device, ts=1010.0)

    # 機器が再起動してカウンタが 0 に戻った状況
    state.uptime_ticks = 500
    for port in state.ports.values():
        port["in"] = 0
        port["out"] = 0
    snapshot = await poller.poll_once(device, ts=1020.0)

    events = await store.list_events()
    assert any(e["kind"] == "device_restart" for e in events)
    # 基準値を捨てているので、巨大な負の差分から誤ったレートは出ない
    assert all(s.in_bps is None for s in snapshot.interface_samples)


async def test_unreachable_device_is_recorded(store):
    state = FakeDevice()
    state.reachable = False
    poller, device = build_poller(store, state)
    snapshot = await poller.poll_once(device)

    assert snapshot.reachable is False
    assert "timeout" in (snapshot.error or "").lower()
    device_row = await store.get_device("fake1")
    assert device_row["status"] == "down"


async def test_interface_filter_excludes_ports(store):
    state = FakeDevice()
    device = DeviceConfig(
        id="fake1", host="127.0.0.1", interface_exclude=r"Port 2$"
    )
    config = AppConfig(devices=[device], polling=PollingConfig(interval_seconds=5.0))
    poller = Poller(config, store, client_factory=lambda d: FakeClient(d, state))

    snapshot = await poller.poll_once(device)
    indexes = {i.if_index for i in (snapshot.interfaces or [])}
    assert indexes == {"1"}
    assert {s.if_index for s in snapshot.interface_samples} == {"1"}


async def test_simulator_produces_usable_snapshots(store):
    config = demo_config()
    poller = Poller(config, store, client_factory=SimulatedSnmpClient)
    device = config.devices[0]

    first = await poller.poll_once(device)
    assert first.reachable in (True, False)  # デモは稀に無応答になる
    if first.reachable:
        assert first.interfaces
        assert first.sys_descr


async def test_broadcast_reaches_listeners(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    received: list[dict] = []

    async def listener(message):
        received.append(message)

    poller.subscribe(listener)
    await poller.poll_once(device)
    assert received and received[0]["type"] == "snapshot"
    assert received[0]["device"]["device_id"] == "fake1"

    poller.unsubscribe(listener)
    await poller.poll_once(device)
    assert len(received) == 1


async def test_counter32_wrap_across_polls(store):
    state = FakeDevice()
    state.supports_ifx = False
    poller, device = build_poller(store, state)

    state.ports["1"]["in"] = 2**32 - 1_000_000
    await poller.poll_once(device, ts=2000.0)
    # 折り返して 1,000,000 オクテット進んだ状態にする
    state.ports["1"]["in"] = 2**32 + 1_000_000
    snapshot = await poller.poll_once(device, ts=2010.0)

    sample = next(s for s in snapshot.interface_samples if s.if_index == "1")
    assert sample.in_bps == 1_600_000  # 2,000,000 オクテット / 10 秒 * 8


async def test_inventory_assigns_categories_and_port_map(store):
    state = FakeDevice()
    poller, device = build_poller(store, state)
    await poller.poll_once(device, ts=1000.0)

    infos = poller.interfaces("fake1")
    assert all(i.category == "physical" for i in infos)

    port_map = poller.port_map("fake1")
    assert port_map is not None
    assert port_map.panels, "物理パネルが作られていません"
