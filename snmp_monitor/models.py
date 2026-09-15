"""ポーリング結果を表すデータ構造。API 応答の形もここで決まる。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .snmp.oids import if_type_label, status_label


@dataclass(slots=True)
class InterfaceInfo:
    """インターフェースの構成情報 (毎回は変わらない値)。"""

    if_index: str
    name: str | None = None
    descr: str | None = None
    alias: str | None = None
    if_type: int | None = None
    speed_bps: int | None = None
    mtu: int | None = None
    mac: str | None = None
    admin_status: int | None = None
    last_change: int | None = None
    #: portmap.classify() が決める区分 (physical / vlan / stack など)
    category: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.descr or f"if{self.if_index}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        data["if_type_label"] = if_type_label(self.if_type)
        data["admin_status_label"] = status_label(self.admin_status)
        return data


@dataclass(slots=True)
class InterfaceSample:
    """1 回のポーリングで得たインターフェースの瞬時値。"""

    if_index: str
    oper_status: int | None = None
    in_bps: float | None = None
    out_bps: float | None = None
    in_error_rate: float | None = None
    out_error_rate: float | None = None
    in_discard_rate: float | None = None
    out_discard_rate: float | None = None
    in_octets: int | None = None
    out_octets: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["oper_status_label"] = status_label(self.oper_status)
        return data


@dataclass(slots=True)
class StorageInfo:
    """メモリ / ディスクの使用状況。"""

    key: str
    descr: str | None
    kind: str  # ram / swap / disk / other
    used_bytes: int | None
    total_bytes: int | None

    @property
    def used_percent(self) -> float | None:
        if not self.total_bytes:
            return None
        return round(100.0 * (self.used_bytes or 0) / self.total_bytes, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["used_percent"] = self.used_percent
        return data


@dataclass(slots=True)
class DeviceSnapshot:
    """デバイス 1 台・1 回分のポーリング結果。"""

    device_id: str
    ts: float
    reachable: bool
    error: str | None = None
    response_ms: float | None = None

    sys_name: str | None = None
    sys_descr: str | None = None
    sys_object_id: str | None = None
    sys_location: str | None = None
    sys_contact: str | None = None
    uptime_ticks: int | None = None

    cpu_percent: float | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None

    #: 構成情報を取り直したポーリングでのみ入る
    interfaces: list[InterfaceInfo] | None = None
    interface_samples: list[InterfaceSample] = field(default_factory=list)
    storages: list[StorageInfo] = field(default_factory=list)

    @property
    def uptime_seconds(self) -> float | None:
        return None if self.uptime_ticks is None else self.uptime_ticks / 100.0

    @property
    def mem_percent(self) -> float | None:
        if not self.mem_total_bytes:
            return None
        return round(100.0 * (self.mem_used_bytes or 0) / self.mem_total_bytes, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "ts": self.ts,
            "reachable": self.reachable,
            "error": self.error,
            "response_ms": self.response_ms,
            "sys_name": self.sys_name,
            "sys_descr": self.sys_descr,
            "sys_object_id": self.sys_object_id,
            "sys_location": self.sys_location,
            "sys_contact": self.sys_contact,
            "uptime_ticks": self.uptime_ticks,
            "uptime_seconds": self.uptime_seconds,
            "cpu_percent": self.cpu_percent,
            "mem_used_bytes": self.mem_used_bytes,
            "mem_total_bytes": self.mem_total_bytes,
            "mem_percent": self.mem_percent,
            "interfaces": [i.to_dict() for i in self.interfaces] if self.interfaces else None,
            "interface_samples": [s.to_dict() for s in self.interface_samples],
            "storages": [s.to_dict() for s in self.storages],
        }


@dataclass(slots=True)
class Event:
    """状態変化 (リンクダウン、デバイス到達不可など) の記録。"""

    ts: float
    device_id: str
    kind: str
    severity: str  # info / warning / critical
    message: str
    if_index: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
