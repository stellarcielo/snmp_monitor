"""デモモード用の仮想 SNMP デバイス。

実機がなくてもダッシュボードの動作を確認できるように、:class:`SnmpClient` と
同じインターフェースでもっともらしい値を返す。
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, DeviceConfig, PollingConfig, SnmpV2cConfig, SnmpV3Config
from .snmp import oids
from .snmp.client import SnmpTimeoutError


@dataclass
class _Port:
    if_index: int
    name: str
    alias: str
    speed_bps: int
    if_type: int = 6
    admin_status: int = 1
    oper_status: int = 1
    base_in_bps: float = 0.0
    base_out_bps: float = 0.0
    in_octets: int = 0
    out_octets: int = 0
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0
    mac: bytes = b"\x00\x00\x00\x00\x00\x00"


@dataclass
class _Profile:
    """仮想デバイスの構成テンプレート。"""

    sys_descr: str
    ports: list[_Port]
    cpu_base: float = 12.0
    mem_total: int = 512 * 1024 * 1024
    mem_base_percent: float = 38.0
    disk_total: int = 8 * 1024**3
    disk_used_percent: float = 22.0
    flaky_port_index: int | None = None


def _mac(seed: int, port: int) -> bytes:
    return bytes([0x02, 0x1A, 0x2B, (seed >> 8) & 0xFF, seed & 0xFF, port & 0xFF])


def _gateway_profile(seed: int) -> _Profile:
    gbe = 1_000_000_000
    ports = [
        _Port(1, "eth0", "WAN", gbe, base_in_bps=180e6, base_out_bps=42e6, mac=_mac(seed, 1)),
        _Port(
            2, "eth1", "LAN トランク", gbe,
            base_in_bps=60e6, base_out_bps=210e6, mac=_mac(seed, 2),
        ),
        _Port(
            3, "eth2", "サーバ接続", gbe,
            base_in_bps=24e6, base_out_bps=31e6, mac=_mac(seed, 3),
        ),
        _Port(
            4, "lo", "", 10_000_000, if_type=24,
            base_in_bps=1e5, base_out_bps=1e5, mac=_mac(seed, 4),
        ),
    ]
    return _Profile(
        sys_descr="Demo Gateway OS 3.2.1 (simulated)",
        ports=ports,
        cpu_base=18.0,
        mem_total=1024 * 1024 * 1024,
        mem_base_percent=46.0,
    )


def _switch_profile(seed: int, port_count: int = 12) -> _Profile:
    ports: list[_Port] = []
    for i in range(1, port_count + 1):
        # 一部のポートは未接続 (リンクダウン) にしておく
        linked = i % 5 != 0
        ports.append(
            _Port(
                if_index=i,
                name=f"Port {i}",
                alias=f"Desk {i:02d}" if linked else "未使用",
                speed_bps=1_000_000_000,
                oper_status=1 if linked else 2,
                base_in_bps=random.uniform(2e6, 90e6) if linked else 0.0,
                base_out_bps=random.uniform(2e6, 70e6) if linked else 0.0,
                mac=_mac(seed, i),
            )
        )
    ports.append(
        _Port(
            if_index=port_count + 1,
            name=f"Port {port_count + 1} (SFP+)",
            alias="Uplink",
            speed_bps=10_000_000_000,
            base_in_bps=420e6,
            base_out_bps=380e6,
            mac=_mac(seed, port_count + 1),
        )
    )
    return _Profile(
        sys_descr="Demo Switch OS 5.0.9 (simulated)",
        ports=ports,
        cpu_base=9.0,
        mem_total=256 * 1024 * 1024,
        mem_base_percent=52.0,
        disk_total=2 * 1024**3,
        flaky_port_index=3,
    )


def _ap_profile(seed: int) -> _Profile:
    ports = [
        _Port(
            1, "eth0", "PoE 上流", 1_000_000_000,
            base_in_bps=45e6, base_out_bps=120e6, mac=_mac(seed, 1),
        ),
        _Port(
            2, "ath0", "2.4GHz", 300_000_000, if_type=71,
            base_in_bps=8e6, base_out_bps=22e6, mac=_mac(seed, 2),
        ),
        _Port(
            3, "ath1", "5GHz", 1_200_000_000, if_type=71,
            base_in_bps=38e6, base_out_bps=96e6, mac=_mac(seed, 3),
        ),
    ]
    return _Profile(
        sys_descr="Demo AP OS 6.4.2 (simulated)",
        ports=ports,
        cpu_base=26.0,
        mem_total=128 * 1024 * 1024,
        mem_base_percent=61.0,
        disk_total=512 * 1024**2,
        disk_used_percent=38.0,
    )


PROFILE_BUILDERS = {
    "gateway": _gateway_profile,
    "switch": _switch_profile,
    "ap": _ap_profile,
}


@dataclass
class _SimState:
    """プロセス内で共有する仮想デバイスの状態。"""

    profile: _Profile
    boot_time: float
    last_update: float
    rng: random.Random
    unreachable_until: float = 0.0
    storages: dict[str, dict[str, Any]] = field(default_factory=dict)


_STATES: dict[str, _SimState] = {}


def _kind_for(device: DeviceConfig) -> str:
    for tag in device.tags:
        if tag in PROFILE_BUILDERS:
            return tag
    lowered = f"{device.id} {device.name or ''}".lower()
    for kind in PROFILE_BUILDERS:
        if kind in lowered:
            return kind
    return "switch"


class SimulatedSnmpClient:
    """:class:`~snmp_monitor.snmp.client.SnmpClient` と同じ API を持つダミー実装。"""

    def __init__(self, device: DeviceConfig) -> None:
        self.device = device
        seed = abs(hash(device.id)) % 65536
        state = _STATES.get(device.id)
        if state is None:
            rng = random.Random(seed)
            builder = PROFILE_BUILDERS[_kind_for(device)]
            now = time.time()
            state = _SimState(
                profile=builder(seed),
                boot_time=now - rng.uniform(3600, 86400 * 40),
                last_update=now,
                rng=rng,
            )
            _STATES[device.id] = state
        self.state = state

    async def close(self) -> None:  # pragma: no cover - 何もしない
        return None

    async def __aenter__(self) -> SimulatedSnmpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    # -- 内部状態の更新 --------------------------------------------------
    def _advance(self) -> float:
        now = time.time()
        state = self.state
        elapsed = max(0.0, now - state.last_update)
        state.last_update = now
        if elapsed == 0.0:
            return now

        rng = state.rng
        # 1 日周期の負荷変動をつくる
        phase = math.sin((now % 86400) / 86400 * 2 * math.pi - math.pi / 2)
        factor = 0.35 + 0.65 * (phase + 1) / 2

        for port in state.profile.ports:
            if port.oper_status != 1:
                continue
            jitter = rng.uniform(0.75, 1.3)
            in_bps = min(port.base_in_bps * factor * jitter, port.speed_bps * 0.95)
            out_bps = min(port.base_out_bps * factor * jitter, port.speed_bps * 0.95)
            port.in_octets = (port.in_octets + int(in_bps / 8 * elapsed)) % (2**64)
            port.out_octets = (port.out_octets + int(out_bps / 8 * elapsed)) % (2**64)
            if rng.random() < 0.03:
                port.in_errors += rng.randint(1, 3)
            if rng.random() < 0.02:
                port.out_discards += rng.randint(1, 5)

        # たまにリンクが上下する様子を再現する
        flaky = state.profile.flaky_port_index
        if flaky is not None and rng.random() < 0.05:
            for port in state.profile.ports:
                if port.if_index == flaky:
                    port.oper_status = 2 if port.oper_status == 1 else 1
        # ごくまれにデバイスごと無応答にする
        if rng.random() < 0.01:
            state.unreachable_until = now + rng.uniform(20, 60)
        return now

    def _check_reachable(self) -> None:
        if time.time() < self.state.unreachable_until:
            raise SnmpTimeoutError("No SNMP response received before timeout (simulated)")

    # -- SnmpClient 互換 API ---------------------------------------------
    async def get(self, oid_map: Mapping[str, str] | Sequence[str]) -> dict[str, Any]:
        self._check_reachable()
        now = self._advance()
        state = self.state
        uptime_ticks = int((now - state.boot_time) * 100)
        values = {
            oids.SYS_DESCR: state.profile.sys_descr.encode(),
            oids.SYS_OBJECT_ID: "1.3.6.1.4.1.99999.1",
            oids.SYS_UPTIME: uptime_ticks,
            oids.SYS_CONTACT: b"noc@example.local",
            oids.SYS_NAME: (self.device.name or self.device.id).encode(),
            oids.SYS_LOCATION: b"Demo Rack / Simulated",
            oids.UCD_SS_CPU_IDLE: int(100 - self._cpu_percent()),
            oids.UCD_MEM_TOTAL_REAL: state.profile.mem_total // 1024,
            oids.UCD_MEM_AVAIL_REAL: int(
                state.profile.mem_total * (1 - self._mem_percent() / 100) / 1024
            ),
            oids.UCD_MEM_BUFFER: 0,
            oids.UCD_MEM_CACHED: 0,
        }
        if isinstance(oid_map, Mapping):
            return {name: values.get(oid) for name, oid in oid_map.items()}
        return {oid: values.get(oid) for oid in oid_map}

    def _cpu_percent(self) -> float:
        base = self.state.profile.cpu_base
        return max(1.0, min(99.0, base + self.state.rng.uniform(-6, 14)))

    def _mem_percent(self) -> float:
        base = self.state.profile.mem_base_percent
        return max(5.0, min(97.0, base + self.state.rng.uniform(-3, 5)))

    async def get_table(
        self, columns: Mapping[str, str], **_: Any
    ) -> dict[str, dict[str, Any]]:
        self._check_reachable()
        self._advance()
        keys = set(columns)

        if keys == set(oids.INTERFACE_INVENTORY_COLUMNS):
            return {
                str(p.if_index): {
                    "descr": p.name.encode(),
                    "if_type": p.if_type,
                    "mtu": 1500,
                    "speed": min(p.speed_bps, 4_294_967_295),
                    "phys_address": p.mac,
                    "admin_status": p.admin_status,
                    "last_change": 0,
                }
                for p in self.state.profile.ports
            }
        if keys == set(oids.INTERFACE_INVENTORY_COLUMNS_X):
            return {
                str(p.if_index): {
                    "name": p.name.encode(),
                    "high_speed": p.speed_bps // 1_000_000,
                    "alias": p.alias.encode(),
                }
                for p in self.state.profile.ports
            }
        if keys == set(oids.INTERFACE_COUNTER_COLUMNS):
            return {
                str(p.if_index): {
                    "oper_status": p.oper_status,
                    "in_octets_32": p.in_octets % (2**32),
                    "out_octets_32": p.out_octets % (2**32),
                    "in_errors": p.in_errors,
                    "out_errors": p.out_errors,
                    "in_discards": p.in_discards,
                    "out_discards": p.out_discards,
                }
                for p in self.state.profile.ports
            }
        if keys == set(oids.INTERFACE_COUNTER_COLUMNS_X):
            return {
                str(p.if_index): {
                    "in_octets_64": p.in_octets,
                    "out_octets_64": p.out_octets,
                }
                for p in self.state.profile.ports
            }
        if keys == set(oids.HR_STORAGE_COLUMNS):
            profile = self.state.profile
            mem_percent = self._mem_percent()
            disk_percent = profile.disk_used_percent + self.state.rng.uniform(-1, 1)
            unit = 4096
            return {
                "1": {
                    "storage_type": oids.HR_STORAGE_TYPE_RAM,
                    "descr": b"Physical memory",
                    "allocation_units": unit,
                    "size": profile.mem_total // unit,
                    "used": int(profile.mem_total * mem_percent / 100) // unit,
                },
                "2": {
                    "storage_type": oids.HR_STORAGE_TYPE_FIXED_DISK,
                    "descr": b"/",
                    "allocation_units": unit,
                    "size": profile.disk_total // unit,
                    "used": int(profile.disk_total * disk_percent / 100) // unit,
                },
            }
        if keys == set(oids.UCD_DISK_COLUMNS):
            return {}
        return {}

    async def walk(self, base_oid: str, **_: Any) -> dict[str, Any]:
        self._check_reachable()
        self._advance()
        if base_oid == oids.HR_PROCESSOR_LOAD:
            cpu = self._cpu_percent()
            cores = 4
            return {
                str(i + 1): max(0, min(100, int(cpu + self.state.rng.uniform(-8, 8))))
                for i in range(cores)
            }
        return {}


DEMO_DEVICES = [
    DeviceConfig(
        id="demo-gateway",
        name="Demo Gateway",
        host="192.0.2.1",
        tags=["gateway", "demo"],
        snmp=SnmpV2cConfig(community="public"),
    ),
    DeviceConfig(
        id="demo-switch-1",
        name="Demo Switch 1F",
        host="192.0.2.11",
        tags=["switch", "demo"],
        snmp=SnmpV3Config(
            username="monitor",
            auth_protocol="sha",
            auth_key="demo-auth-key",
            priv_protocol="aes",
            priv_key="demo-priv-key",
        ),
    ),
    DeviceConfig(
        id="demo-switch-2",
        name="Demo Switch 2F",
        host="192.0.2.12",
        tags=["switch", "demo"],
        snmp=SnmpV2cConfig(community="public"),
    ),
    DeviceConfig(
        id="demo-ap-1",
        name="Demo AP Lounge",
        host="192.0.2.21",
        tags=["ap", "demo"],
        snmp=SnmpV2cConfig(community="public"),
    ),
]


def demo_config(base: AppConfig | None = None) -> AppConfig:
    """デモ用のデバイス一式を持つ設定を返す。"""
    config = base.model_copy(deep=True) if base else AppConfig()
    config.demo_mode = True
    if not config.devices:
        config.devices = [d.model_copy(deep=True) for d in DEMO_DEVICES]
    if config.polling == PollingConfig():
        config.polling = PollingConfig(interval_seconds=5.0, inventory_every_n_polls=12)
    return config


def reset_simulator_state() -> None:
    """テスト用に仮想デバイスの状態を初期化する。"""
    _STATES.clear()
