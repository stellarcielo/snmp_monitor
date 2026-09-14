"""SQLite への保存・集約・削除のテスト。"""

from __future__ import annotations

import time

from snmp_monitor.config import DeviceConfig, StorageConfig
from snmp_monitor.models import DeviceSnapshot, Event, InterfaceInfo, InterfaceSample, StorageInfo
from snmp_monitor.storage.timeseries import RAW, RES_1H, RES_5M, choose_resolution


def snapshot(device_id="d1", ts=1000.0, in_bps=1e6, out_bps=2e6, reachable=True):
    return DeviceSnapshot(
        device_id=device_id,
        ts=ts,
        reachable=reachable,
        response_ms=12.5,
        sys_name="switch01",
        sys_descr="Test switch",
        uptime_ticks=123456,
        cpu_percent=25.0,
        mem_used_bytes=512,
        mem_total_bytes=1024,
        interfaces=[
            InterfaceInfo(if_index="1", name="Port 1", alias="uplink", speed_bps=10**9, if_type=6)
        ],
        interface_samples=[
            InterfaceSample(if_index="1", oper_status=1, in_bps=in_bps, out_bps=out_bps)
        ],
        storages=[
            StorageInfo(key="hr:1", descr="RAM", kind="ram", used_bytes=512, total_bytes=1024)
        ],
    )


def test_choose_resolution():
    assert choose_resolution(3600) == RAW
    assert choose_resolution(24 * 3600) == RES_5M
    assert choose_resolution(30 * 86400) == RES_1H


async def test_register_device_is_idempotent(store):
    device = DeviceConfig(id="d1", name="スイッチ", host="10.0.0.1", tags=["switch"])
    await store.register_device(device)
    await store.register_device(device)
    devices = await store.list_devices()
    assert len(devices) == 1
    assert devices[0]["name"] == "スイッチ"
    assert devices[0]["tags"] == ["switch"]


async def test_save_snapshot_writes_all_tables(store):
    await store.save_snapshot(snapshot())

    device = await store.get_device("d1")
    assert device["status"] == "up"
    assert device["sys_name"] == "switch01"
    assert device["uptime_seconds"] == 1234.56

    interfaces = await store.list_interfaces("d1")
    assert interfaces[0]["name"] == "Port 1"
    assert interfaces[0]["oper_status"] == 1

    storages = await store.list_storages("d1")
    assert storages[0]["used_percent"] == 50.0

    history = await store.interface_history("d1", "1", 0, 2000, RAW)
    assert history["points"][0]["in_bps"] == 1e6


async def test_unreachable_snapshot_marks_device_down(store):
    await store.save_snapshot(snapshot(ts=1000.0))
    down = DeviceSnapshot(device_id="d1", ts=1010.0, reachable=False, error="timeout")
    await store.save_snapshot(down)

    device = await store.get_device("d1")
    assert device["status"] == "down"
    assert device["last_error"] == "timeout"
    # 応答がなくても、最後に取得できた情報は残す
    assert device["sys_name"] == "switch01"


async def test_device_traffic_history_sums_interfaces(store):
    snap = snapshot()
    snap.interface_samples.append(
        InterfaceSample(if_index="2", oper_status=1, in_bps=5e5, out_bps=1e5)
    )
    await store.save_snapshot(snap)
    result = await store.device_traffic_history("d1", 0, 2000, RAW)
    assert result["points"][0]["in_bps"] == 1.5e6


async def test_recent_traffic_groups_by_device(store):
    await store.save_snapshot(snapshot(device_id="d1", ts=1000.0))
    await store.save_snapshot(snapshot(device_id="d2", ts=1000.0))
    data = await store.recent_traffic(0, 2000)
    assert set(data) == {"d1", "d2"}
    assert data["d1"][0]["in_bps"] == 1e6


async def test_rollup_creates_5m_buckets(store):
    base = 1_700_000_000.0
    base -= base % 300  # バケット境界に揃える
    for i in range(5):
        await store.save_snapshot(snapshot(ts=base + i * 30, in_bps=(i + 1) * 1e6))

    await store.rollup(now=base + 600)

    rolled = await store.interface_history("d1", "1", base - 600, base + 600, RES_5M)
    assert len(rolled["points"]) == 1
    point = rolled["points"][0]
    assert point["in_bps"] == 3e6  # (1+2+3+4+5)/5 Mbps
    assert point["in_bps_max"] == 5e6  # ピークは max として残す


async def test_rollup_is_incremental_and_idempotent(store):
    base = 1_700_000_000.0
    base -= base % 300
    await store.save_snapshot(snapshot(ts=base + 10, in_bps=1e6))
    await store.rollup(now=base + 600)
    await store.rollup(now=base + 600)  # 2 回目は何も増えない
    rolled = await store.interface_history("d1", "1", base - 600, base + 600, RES_5M)
    assert len(rolled["points"]) == 1


async def test_prune_removes_old_raw_data(store):
    now = time.time()
    store.config = StorageConfig(raw_retention_hours=1, rollup_5m_retention_days=1)
    await store.save_snapshot(snapshot(ts=now - 7200))  # 2 時間前 -> 消える
    await store.save_snapshot(snapshot(ts=now - 60))  # 1 分前 -> 残る

    await store.prune(now=now)

    points = (await store.interface_history("d1", "1", 0, now + 1, RAW))["points"]
    assert len(points) == 1
    assert points[0]["ts"] > now - 120


async def test_events_are_stored_and_listed(store):
    await store.add_events(
        [
            Event(ts=1.0, device_id="d1", kind="link_down", severity="warning", message="down"),
            Event(ts=2.0, device_id="d2", kind="device_up", severity="info", message="up"),
        ]
    )
    all_events = await store.list_events()
    assert [e["message"] for e in all_events] == ["up", "down"]  # 新しい順

    only_d1 = await store.list_events(device_id="d1")
    assert len(only_d1) == 1
