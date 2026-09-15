"""インターフェースの区分判定と、物理ポートの配置推定。

SNMP からはポートパネルの実際の形状を知る手段がないため、``ifName`` の
番号体系 (例: ``GigabitEthernet1/0/24``) から推定する。推定が合わない機種は
設定ファイル (``port_layout``) で上書きできる。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .config import PortGroupConfig, PortLayoutConfig
from .models import InterfaceInfo

# --- 区分 ---------------------------------------------------------------
PHYSICAL = "physical"
UPLINK = "uplink"
MGMT = "mgmt"
LAG = "lag"
VLAN = "vlan"
STACK = "stack"
VIRTUAL = "virtual"
OTHER = "other"

CATEGORY_LABELS = {
    PHYSICAL: "物理ポート",
    UPLINK: "アップリンク / SFP",
    MGMT: "管理ポート",
    LAG: "LAG / ポートチャネル",
    VLAN: "VLAN インターフェース",
    STACK: "スタックポート",
    VIRTUAL: "仮想インターフェース",
    OTHER: "その他",
}

#: 物理パネルとして描画する区分
PANEL_CATEGORIES = (PHYSICAL, UPLINK, MGMT)

#: 区分の表示順
CATEGORY_ORDER = (PHYSICAL, UPLINK, MGMT, LAG, VLAN, STACK, VIRTUAL, OTHER)

# ifType (IANAifType-MIB)
IF_TYPE_ETHERNET = 6
IF_TYPE_LOOPBACK = 24
IF_TYPE_PROP_VIRTUAL = 53
IF_TYPE_WIRELESS = 71
IF_TYPE_TUNNEL = 131
IF_TYPE_L2VLAN = 135
IF_TYPE_L3VLAN = 136
IF_TYPE_LAG = 161

# --- 名前パターン -------------------------------------------------------
# スタック相互接続 (Cisco StackPort / StackSub, Juniper VCP, HPE IRF)
_STACK_RE = re.compile(r"(^|[^a-z])(stack|stackport|stacksub|vcp|irf)([^a-z]|\d|$)", re.I)
# リンクアグリゲーション
_LAG_RE = re.compile(
    r"^(port-?channel|po\d+$|bond\d+$|ae\d+$|lag\d*$|trk\d+$|trunk\d+$|team\d*$)", re.I
)
# VLAN / SVI / サブインターフェース (Gi0/1.100)
_VLAN_RE = re.compile(r"^(vlan|svi|irb|bvi)[\s._-]*\d*$", re.I)
_SUBIF_RE = re.compile(r"^.+\.\d+$")
# ソフトウェア的な仮想インターフェース
_VIRTUAL_RE = re.compile(
    r"^(lo|lo\d+|loopback\d*|null\d*|tun\d*|tunnel\d*|gre\d*|docker\d*|veth.*|br-.*|"
    r"virbr\d*|dummy\d*|tap\d*|sit\d*|ip6tnl\d*|nflog|ifb\d*|gretap.*|wg\d*)$",
    re.I,
)
# 光ポート / アップリンクを示しやすい名前
_UPLINK_NAME_RE = re.compile(
    r"(sfp|xfp|qsfp|uplink|^xe-|^et-|^te\d|tengig|twentyfivegig|fortygig|"
    r"hundredgig|^hu\d|^fo\d|^twe\d)",
    re.I,
)

# OOB 管理ポート (Cisco Nexus mgmt0 / Arista Ma1 / Juniper fxp0・me0 など)
_MGMT_NAME_RE = re.compile(
    r"^(mgmt\d*|management\d*|ma\d+|me\d+|fxp\d+|vme\d*|oob\w*)$", re.I
)

#: ``プレフィックス + 数値階層`` に分解するパターン (例: Gi1/0/24, ge-0/0/12, Port 24)
_PORT_NAME_RE = re.compile(
    r"^(?P<prefix>[A-Za-z][A-Za-z_\- ]*?)?\s*(?P<numbers>\d+(?:[/:]\d+)*)$"
)

#: この速度以上は、他に低速ポートがあればアップリンクとみなす
UPLINK_SPEED_THRESHOLD = 10_000_000_000


def _names(info: InterfaceInfo) -> tuple[str, ...]:
    return tuple(n for n in (info.name, info.descr) if n)


def classify(
    info: InterfaceInfo, *, uplink_speed: int | None = None
) -> str:
    """インターフェース 1 本の区分を判定する。

    ``uplink_speed`` にはデバイス内でアップリンクとみなす速度を渡す
    (:func:`detect_uplink_speed` で求める)。
    """
    names = _names(info)
    if_type = info.if_type

    for name in names:
        if _MGMT_NAME_RE.match(name):
            return MGMT
        if _STACK_RE.search(name):
            return STACK
        if _LAG_RE.match(name):
            return LAG
        if _VLAN_RE.match(name):
            return VLAN
        if _VIRTUAL_RE.match(name):
            return VIRTUAL

    if if_type == IF_TYPE_LAG:
        return LAG
    if if_type in (IF_TYPE_L2VLAN, IF_TYPE_L3VLAN):
        return VLAN
    if if_type in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL):
        return VIRTUAL

    # サブインターフェース (Gi0/1.100) は VLAN 側にまとめる
    if any(_SUBIF_RE.match(name) for name in names):
        return VLAN

    if if_type in (IF_TYPE_ETHERNET, IF_TYPE_WIRELESS):
        if any(_UPLINK_NAME_RE.search(name) for name in names):
            return UPLINK
        if uplink_speed and info.speed_bps and info.speed_bps >= uplink_speed:
            return UPLINK
        return PHYSICAL

    if if_type == IF_TYPE_PROP_VIRTUAL:
        return VIRTUAL
    if if_type is None:
        # ifType が取れない機器でも、名前から番号が読めれば物理ポートとして扱う
        return PHYSICAL if any(parse_port_name(n) for n in names) else OTHER
    return OTHER


def detect_uplink_speed(interfaces: list[InterfaceInfo]) -> int | None:
    """デバイス内で「アップリンク扱いにする速度」を決める。

    全ポートが同じ速度の機器 (10G スイッチなど) で全部がアップリンクに
    なってしまわないよう、最速かつ少数派である場合にのみ採用する。
    """
    speeds = [
        i.speed_bps
        for i in interfaces
        if i.speed_bps and i.if_type in (IF_TYPE_ETHERNET, IF_TYPE_WIRELESS)
    ]
    if not speeds:
        return None
    counts = Counter(speeds)
    fastest = max(counts)
    if fastest < UPLINK_SPEED_THRESHOLD:
        return None
    # 最速ポートが全体の 1/3 を超えるなら「そういう機種」とみなして分けない
    if counts[fastest] * 3 > len(speeds):
        return None
    return fastest


# --- 名前の分解 ---------------------------------------------------------
@dataclass(frozen=True)
class ParsedName:
    """``Gi1/0/24`` のような名前を分解した結果。"""

    prefix: str
    numbers: tuple[int, ...]

    @property
    def unit(self) -> tuple[int, ...]:
        """末尾のポート番号を除いた、ユニット / モジュールの識別子。"""
        return self.numbers[:-1]

    @property
    def port_number(self) -> int:
        return self.numbers[-1]


def parse_port_name(name: str | None) -> ParsedName | None:
    """インターフェース名を ``プレフィックス`` と ``数値階層`` に分解する。"""
    if not name:
        return None
    match = _PORT_NAME_RE.match(name.strip())
    if not match:
        return None
    prefix = (match.group("prefix") or "").strip(" -_").lower()
    numbers = tuple(int(n) for n in re.split(r"[/:]", match.group("numbers")))
    return ParsedName(prefix=prefix, numbers=numbers)


def _parse_any(info: InterfaceInfo) -> ParsedName | None:
    for name in _names(info):
        parsed = parse_port_name(name)
        if parsed:
            return parsed
    return None


# --- 配置 ---------------------------------------------------------------
@dataclass
class PanelSection:
    """パネル内の 1 区画 (本体側 / SFP 側)。"""

    kind: str
    rows: list[list[str | None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "rows": self.rows}


@dataclass
class PortPanel:
    """物理ポートのまとまり (スタックメンバや筐体の単位)。"""

    key: str
    name: str | None
    sections: list[PanelSection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "sections": [s.to_dict() for s in self.sections],
        }


@dataclass
class CategorySection:
    """物理パネル以外の区分 (VLAN・スタックなど)。"""

    category: str
    if_indexes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "if_indexes": self.if_indexes}


@dataclass
class PortMap:
    panels: list[PortPanel] = field(default_factory=list)
    sections: list[CategorySection] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "panels": [p.to_dict() for p in self.panels],
            "sections": [s.to_dict() for s in self.sections],
            "categories": self.categories,
        }


def _sort_key(entry: tuple[InterfaceInfo, ParsedName | None]) -> tuple[Any, ...]:
    info, parsed = entry
    if parsed:
        return (0, parsed.numbers)
    try:
        return (1, (int(info.if_index),))
    except ValueError:
        return (2, (0,), info.if_index)


def _layout_rows(
    entries: list[tuple[InterfaceInfo, ParsedName | None]],
    rows: int,
    order: str,
) -> list[list[str | None]]:
    """1 区画分のポートを ``rows`` 段に並べる。

    ``odd-top`` では、実際の RJ45 スイッチと同じく奇数番号を上段に置く。
    番号が飛んでいる場合は、その位置を空きスロット (None) として残す。
    """
    if not entries:
        return []
    if rows <= 1 or len(entries) < 2:
        return [[info.if_index for info, _ in entries]]

    numbers = [parsed.port_number for _, parsed in entries if parsed]
    use_numbers = order == "odd-top" and len(numbers) == len(entries)

    if not use_numbers:
        # 番号が読めない場合は、並び順のまま上下に振り分ける
        indexes = [info.if_index for info, _ in entries]
        return [indexes[i::rows] for i in range(rows)]

    lowest, highest = min(numbers), max(numbers)
    span = highest - lowest + 1
    # 欠番が多すぎるときは空きスロットを作らず詰める
    keep_gaps = span <= len(entries) * 2

    by_number = {parsed.port_number: info.if_index for info, parsed in entries if parsed}
    sequence: list[str | None]
    if keep_gaps:
        sequence = [by_number.get(n) for n in range(lowest, highest + 1)]
    else:
        sequence = [by_number[n] for n in sorted(by_number)]

    # 先頭の番号を上段に合わせる (0 始まりの機器でも段がずれないように)
    return [sequence[i::rows] for i in range(rows)]


def _default_rows(count: int, configured: int | None) -> int:
    if configured:
        return configured
    # 8 ポート以上ある機器は 2 段組みが一般的
    return 2 if count >= 8 else 1


#: 本体パネルとみなす最低ポート数 (これ以上あれば「本体がある」と判断する)
MIN_MAIN_PANEL_PORTS = 8

#: 本体から切り離されたシャーシが、この本数以下なら管理ポートとみなす
MAX_MANAGEMENT_PORTS = 2


def _detect_management_ports(
    entries: list[tuple[InterfaceInfo, ParsedName | None]], categories: dict[str, str]
) -> set[str]:
    """本体から切り離された少数のポートを管理ポートとみなす。

    Catalyst の ``GigabitEthernet0/0`` は名前の階層としては本体 (``Gi1/0/N``) と
    別シャーシになるため、そのままではユニット 0 という不自然なパネルができる。
    「本体と呼べる大きさのパネルがあり、そこから外れたポートが数本だけ」という
    形なら OOB 管理ポートと判断する。
    """
    groups: dict[tuple[int, ...], list[str]] = {}
    for info, parsed in entries:
        if categories[info.if_index] not in (PHYSICAL, UPLINK):
            continue
        groups.setdefault(_chassis_of(parsed), []).append(info.if_index)

    if len(groups) < 2:
        return set()
    if max(len(indexes) for indexes in groups.values()) < MIN_MAIN_PANEL_PORTS:
        return set()

    management: set[str] = set()
    for indexes in groups.values():
        if len(indexes) <= MAX_MANAGEMENT_PORTS:
            management.update(indexes)
    return management


def _chassis_of(parsed: ParsedName | None) -> tuple[int, ...]:
    """パネルを分ける単位 (スタックメンバ / シャーシ) を求める。

    ``Gi1/0/24`` と ``Te1/1/1`` は同じ筐体の本体側と SFP 側なので、
    先頭の番号だけを見て同じパネルにまとめる。
    """
    if parsed is None or not parsed.unit:
        return ()
    return parsed.unit[:1]


def _build_panels(
    entries: list[tuple[InterfaceInfo, ParsedName | None]],
    categories: dict[str, str],
    layout: PortLayoutConfig | None,
) -> list[PortPanel]:
    """物理ポートを筐体ごとのパネルに組み立てる。"""
    management = [e for e in entries if categories[e[0].if_index] == MGMT]
    entries = [e for e in entries if categories[e[0].if_index] != MGMT]

    chassis: dict[tuple[int, ...], list[tuple[InterfaceInfo, ParsedName | None]]] = {}
    for info, parsed in entries:
        chassis.setdefault(_chassis_of(parsed), []).append((info, parsed))

    keys = sorted(chassis, key=lambda u: (len(u), u))
    panels: list[PortPanel] = []
    for key in keys:
        members = sorted(chassis[key], key=_sort_key)
        main = [e for e in members if categories[e[0].if_index] == PHYSICAL]
        sfp = [e for e in members if categories[e[0].if_index] == UPLINK]

        sections: list[PanelSection] = []
        if main:
            rows = _default_rows(len(main), layout.rows if layout else None)
            order = layout.order if layout else "odd-top"
            sections.append(PanelSection("main", _layout_rows(main, rows, order)))
        if sfp:
            rows = 2 if len(sfp) >= 4 else 1
            sections.append(PanelSection("sfp", _layout_rows(sfp, rows, "odd-top")))
        if sections:
            panels.append(
                PortPanel(
                    key="-".join(str(n) for n in key) or "main",
                    name=f"ユニット {key[0]}" if key and len(keys) > 1 else None,
                    sections=sections,
                )
            )

    if management:
        # 管理ポートは本体パネルの左端に置く (SFP が右端にあるのと対になる)
        rows = _layout_rows(sorted(management, key=_sort_key), 1, "sequential")
        section = PanelSection("mgmt", rows)
        if panels:
            panels[0].sections.insert(0, section)
        else:
            panels.append(PortPanel(key="mgmt", name=None, sections=[section]))
    return panels


def _build_configured_panels(
    entries: list[tuple[InterfaceInfo, ParsedName | None]],
    groups: list[PortGroupConfig],
) -> tuple[list[PortPanel], set[str]]:
    """設定ファイルで明示されたグループどおりにパネルを組み立てる。"""
    panels: list[PortPanel] = []
    used: set[str] = set()
    for group in groups:
        members = [
            entry
            for entry in entries
            if entry[0].if_index not in used
            and group.matches(entry[0].if_index, _port_number(entry))
        ]
        if not members:
            continue
        members.sort(key=_sort_key)
        used.update(info.if_index for info, _ in members)
        rows = _default_rows(len(members), group.rows)
        panels.append(
            PortPanel(
                key=group.name,
                name=group.name,
                sections=[PanelSection("main", _layout_rows(members, rows, group.order))],
            )
        )
    return panels, used


def _override_by_name(
    overrides: dict[str, str], interfaces: list[InterfaceInfo], if_index: str
) -> str | None:
    """ifName を使った区分の強制指定を引く。"""
    info = next((i for i in interfaces if i.if_index == if_index), None)
    return overrides.get(info.name or "") if info else None


def _port_number(entry: tuple[InterfaceInfo, ParsedName | None]) -> int | None:
    _, parsed = entry
    return parsed.port_number if parsed else None


def build_port_map(
    interfaces: list[InterfaceInfo], layout: PortLayoutConfig | None = None
) -> PortMap:
    """インターフェース一覧から、区分と物理配置を推定する。"""
    uplink_speed = detect_uplink_speed(interfaces)
    overrides = layout.categories if layout else {}

    categories: dict[str, str] = {}
    for info in interfaces:
        forced = overrides.get(info.if_index) or overrides.get(info.name or "")
        categories[info.if_index] = forced or classify(info, uplink_speed=uplink_speed)

    entries = [(info, _parse_any(info)) for info in interfaces]
    # 名前だけでは分からない管理ポートを、構成の形から見つける
    for if_index in _detect_management_ports(entries, categories):
        if not (overrides.get(if_index) or _override_by_name(overrides, interfaces, if_index)):
            categories[if_index] = MGMT

    panel_entries = [e for e in entries if categories[e[0].if_index] in PANEL_CATEGORIES]

    if layout and layout.groups:
        panels, used = _build_configured_panels(panel_entries, layout.groups)
        remaining = [e for e in panel_entries if e[0].if_index not in used]
        panels.extend(_build_panels(remaining, categories, layout))
    else:
        panels = _build_panels(panel_entries, categories, layout)

    sections: list[CategorySection] = []
    for category in CATEGORY_ORDER:
        if category in PANEL_CATEGORIES:
            continue
        members = sorted(
            (e for e in entries if categories[e[0].if_index] == category), key=_sort_key
        )
        if members:
            sections.append(
                CategorySection(
                    category=category, if_indexes=[info.if_index for info, _ in members]
                )
            )

    return PortMap(panels=panels, sections=sections, categories=categories)
