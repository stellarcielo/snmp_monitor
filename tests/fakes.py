"""テスト用の疑似 SNMP エージェント。

SnmpClient と同じインターフェースを持つので、デモモードを使わずに
実機と同じコードパス (poller -> api) を通したテストが書ける。
"""

from __future__ import annotations

from typing import Any

from snmp_monitor.config import DeviceConfig
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
                    "if_type": port.get("if_type", 6),
                    "mtu": 1500,
                    "speed": min(port.get("speed", 1_000_000_000), 4_294_967_295),
                    "phys_address": bytes([0, 0, 0, 0, 0, int(index) % 256]),
                    "admin_status": 1,
                    "last_change": 0,
                }
                for index, port in self.state.ports.items()
            }
        if keys == set(oids.INTERFACE_INVENTORY_COLUMNS_X):
            if not self.state.supports_ifx:
                raise SnmpTimeoutError("ifXTable is not supported")
            return {
                index: {
                    "name": port.get("name", f"Port {index}").encode(),
                    "high_speed": port.get("speed", 1_000_000_000) // 1_000_000,
                    "alias": b"",
                }
                for index, port in self.state.ports.items()
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



def cisco_switch_device(port_count: int = 8, units: int = 1) -> FakeDevice:
    """Cisco 風の命名を持つスイッチ (ポートマップの確認用)。"""
    device = FakeDevice()
    device.ports = {}
    index = 1
    # Catalyst の OOB 管理ポート
    device.ports[str(index)] = {
        "oper": 1, "in": 0, "out": 0, "errors": 0,
        "name": "Gi0/0", "speed": 1_000_000_000, "if_type": 6,
    }
    index += 1
    for unit in range(1, units + 1):
        for port in range(1, port_count + 1):
            device.ports[str(index)] = {
                "oper": 1,
                "in": 0,
                "out": 0,
                "errors": 0,
                "name": f"GigabitEthernet{unit}/0/{port}",
                "speed": 1_000_000_000,
                "if_type": 6,
            }
            index += 1
        device.ports[str(index)] = {
            "oper": 1, "in": 0, "out": 0, "errors": 0,
            "name": f"TenGigabitEthernet{unit}/1/1",
            "speed": 10_000_000_000, "if_type": 6,
        }
        index += 1
    for name, if_type in (
        ("Port-channel1", 161),
        ("Vlan100", 136),
        ("StackPort1", 6),
        ("Loopback0", 24),
    ):
        device.ports[str(index)] = {
            "oper": 1, "in": 0, "out": 0, "errors": 0,
            "name": name, "speed": 1_000_000_000, "if_type": if_type,
        }
        index += 1
    return device
