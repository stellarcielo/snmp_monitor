"""インターフェースの区分判定と物理配置の推定に関するテスト。"""

from __future__ import annotations

import typing

import pytest

from snmp_monitor.config import PortCategory, PortGroupConfig, PortLayoutConfig
from snmp_monitor.models import InterfaceInfo
from snmp_monitor.portmap import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    LAG,
    PHYSICAL,
    STACK,
    UPLINK,
    VIRTUAL,
    VLAN,
    build_port_map,
    classify,
    detect_uplink_speed,
    parse_port_name,
)

GBE = 1_000_000_000
TEN_GBE = 10_000_000_000


def iface(index, name, if_type=6, speed=GBE, descr=None):
    return InterfaceInfo(
        if_index=str(index),
        name=name,
        descr=descr if descr is not None else name,
        if_type=if_type,
        speed_bps=speed,
    )


def names(port_map, interfaces, if_indexes):
    by_index = {i.if_index: i for i in interfaces}
    return [by_index[i].name if i else None for i in if_indexes]


# --- 区分の判定 ---------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "if_type", "expected"),
    [
        ("GigabitEthernet1/0/1", 6, PHYSICAL),
        ("Port 12", 6, PHYSICAL),
        ("eth0", 6, PHYSICAL),
        ("ge-0/0/12", 6, PHYSICAL),
        ("Port-channel1", 6, LAG),
        ("Po1", 6, LAG),
        ("bond0", 6, LAG),
        ("ae0", 6, LAG),
        ("lag1", 6, LAG),
        ("Vlan100", 6, VLAN),
        ("vlan 10", 6, VLAN),
        ("irb", 6, VLAN),
        ("GigabitEthernet0/1.100", 6, VLAN),
        ("StackPort1", 6, STACK),
        ("StackSub-St1-1", 6, STACK),
        ("vcp-0/0/0", 6, STACK),
        ("lo", 6, VIRTUAL),
        ("Loopback0", 6, VIRTUAL),
        ("docker0", 6, VIRTUAL),
        ("veth1a2b3c", 6, VIRTUAL),
        ("tun0", 6, VIRTUAL),
        ("TenGigabitEthernet1/1/1", 6, UPLINK),
        ("xe-0/0/1", 6, UPLINK),
        ("Port 25 (SFP+)", 6, UPLINK),
    ],
)
def test_classify_by_name(name, if_type, expected):
    assert classify(iface(1, name, if_type)) == expected


@pytest.mark.parametrize(
    ("if_type", "expected"),
    [(161, LAG), (135, VLAN), (136, VLAN), (24, VIRTUAL), (131, VIRTUAL)],
)
def test_classify_by_if_type(if_type, expected):
    # 名前から判断できなくても ifType で分類できる
    assert classify(iface(1, "unknown-name-9", if_type)) == expected


def test_wireless_is_physical():
    assert classify(iface(1, "ath0", if_type=71)) == PHYSICAL


def test_unknown_if_type_with_numbered_name_is_physical():
    assert classify(iface(1, "Port 4", if_type=None)) == PHYSICAL


def test_unknown_if_type_without_numbers_is_other():
    assert classify(iface(1, "weird-thing", if_type=None)) == "other"


def test_high_speed_port_becomes_uplink_when_minority():
    interfaces = [iface(n, f"Ethernet{n}") for n in range(1, 13)]
    interfaces.append(iface(13, "Ethernet13", speed=TEN_GBE))
    uplink_speed = detect_uplink_speed(interfaces)
    assert uplink_speed == TEN_GBE
    assert classify(interfaces[-1], uplink_speed=uplink_speed) == UPLINK
    assert classify(interfaces[0], uplink_speed=uplink_speed) == PHYSICAL


def test_all_ports_same_speed_have_no_uplink():
    # 全ポートが 10G の機器で、全部がアップリンク扱いにならないこと
    interfaces = [iface(n, f"Ethernet1/{n}", speed=TEN_GBE) for n in range(1, 13)]
    assert detect_uplink_speed(interfaces) is None
    port_map = build_port_map(interfaces)
    assert set(port_map.categories.values()) == {PHYSICAL}


def test_gigabit_only_device_has_no_uplink():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 9)]
    assert detect_uplink_speed(interfaces) is None


def test_category_constants_match_config_literal():
    # config の Literal と portmap の定数がずれていないこと
    assert set(typing.get_args(PortCategory)) == set(CATEGORY_ORDER)
    assert set(CATEGORY_LABELS) == set(CATEGORY_ORDER)


# --- 名前の分解 ---------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "prefix", "numbers"),
    [
        ("GigabitEthernet1/0/24", "gigabitethernet", (1, 0, 24)),
        ("Gi1/0/24", "gi", (1, 0, 24)),
        ("Port 24", "port", (24,)),
        ("eth0", "eth", (0,)),
        ("ge-0/0/12", "ge", (0, 0, 12)),
        ("Ethernet1/24", "ethernet", (1, 24)),
        ("xe-0/1/0:1", "xe", (0, 1, 0, 1)),
        ("24", "", (24,)),
    ],
)
def test_parse_port_name(name, prefix, numbers):
    parsed = parse_port_name(name)
    assert parsed is not None
    assert parsed.prefix == prefix
    assert parsed.numbers == numbers


@pytest.mark.parametrize("name", ["", None, "Vlan", "Gi0/1.100", "eth0-backup"])
def test_parse_port_name_rejects_non_ports(name):
    assert parse_port_name(name) is None


def test_lag_name_parses_but_is_excluded_by_category():
    # Port-channel1 は名前としては分解できるが、区分が LAG なのでパネルには並ばない
    assert parse_port_name("Port-channel1") is not None
    interfaces = [iface(1, "Port 1"), iface(2, "Port-channel1", if_type=161)]
    port_map = build_port_map(interfaces)
    panel_indexes = [
        i for s in port_map.panels[0].sections for row in s.rows for i in row if i
    ]
    assert panel_indexes == ["1"]


def test_parsed_name_unit_and_port():
    parsed = parse_port_name("GigabitEthernet2/0/48")
    assert parsed.unit == (2, 0)
    assert parsed.port_number == 48


# --- 物理配置 -----------------------------------------------------------
def test_odd_ports_go_to_the_top_row():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 9)]
    port_map = build_port_map(interfaces)

    rows = port_map.panels[0].sections[0].rows
    assert len(rows) == 2
    assert names(port_map, interfaces, rows[0]) == ["Port 1", "Port 3", "Port 5", "Port 7"]
    assert names(port_map, interfaces, rows[1]) == ["Port 2", "Port 4", "Port 6", "Port 8"]


def test_zero_based_names_keep_alignment():
    # eth0 始まりでも、最初のポートが上段に来る
    interfaces = [iface(n + 1, f"eth{n}") for n in range(8)]
    rows = build_port_map(interfaces).panels[0].sections[0].rows
    assert names(build_port_map(interfaces), interfaces, rows[0]) == [
        "eth0",
        "eth2",
        "eth4",
        "eth6",
    ]


def test_few_ports_stay_on_one_row():
    interfaces = [iface(n, f"eth{n}") for n in range(1, 4)]
    rows = build_port_map(interfaces).panels[0].sections[0].rows
    assert len(rows) == 1


def test_missing_port_numbers_leave_empty_slots():
    # 5, 6 番が存在しない場合、その位置を空けて実機の並びに合わせる
    interfaces = [iface(n, f"Port {n}") for n in (1, 2, 3, 4, 7, 8, 9, 10)]
    port_map = build_port_map(interfaces)
    rows = port_map.panels[0].sections[0].rows
    assert names(port_map, interfaces, rows[0]) == ["Port 1", "Port 3", None, "Port 7", "Port 9"]
    assert names(port_map, interfaces, rows[1]) == ["Port 2", "Port 4", None, "Port 8", "Port 10"]


def test_sparse_numbering_is_packed_instead_of_padded():
    # 番号が大きく飛ぶ機器では、空きスロットだらけにせず詰める
    interfaces = [iface(n, f"Port {n}") for n in (1, 2, 101, 102, 201, 202, 301, 302)]
    rows = build_port_map(interfaces).panels[0].sections[0].rows
    assert all(index is not None for row in rows for index in row)
    assert sum(len(row) for row in rows) == 8


def test_stack_members_are_separate_panels():
    interfaces = [iface(n, f"GigabitEthernet1/0/{n}") for n in range(1, 25)]
    interfaces += [iface(100 + n, f"GigabitEthernet2/0/{n}") for n in range(1, 25)]
    port_map = build_port_map(interfaces)

    assert [p.key for p in port_map.panels] == ["1", "2"]
    assert [p.name for p in port_map.panels] == ["ユニット 1", "ユニット 2"]


def test_sfp_ports_share_the_panel_of_their_chassis():
    # Te1/1/1 は Gi1/0/x と同じ筐体なので、同じパネルの SFP 区画に入る
    interfaces = [iface(n, f"GigabitEthernet1/0/{n}") for n in range(1, 25)]
    interfaces += [
        iface(100 + n, f"TenGigabitEthernet1/1/{n}", speed=TEN_GBE) for n in (1, 2)
    ]
    port_map = build_port_map(interfaces)

    assert len(port_map.panels) == 1
    kinds = [s.kind for s in port_map.panels[0].sections]
    assert kinds == ["main", "sfp"]
    sfp_rows = port_map.panels[0].sections[1].rows
    assert sum(len(row) for row in sfp_rows) == 2


def test_single_panel_has_no_name():
    interfaces = [iface(n, f"GigabitEthernet1/0/{n}") for n in range(1, 9)]
    assert build_port_map(interfaces).panels[0].name is None


def test_non_physical_interfaces_go_to_sections():
    interfaces = [
        iface(1, "GigabitEthernet1/0/1"),
        iface(2, "Port-channel1", if_type=161),
        iface(3, "Vlan100", if_type=136),
        iface(4, "StackPort1"),
        iface(5, "Loopback0", if_type=24),
    ]
    port_map = build_port_map(interfaces)

    assert [s.category for s in port_map.sections] == [LAG, VLAN, STACK, VIRTUAL]
    assert sum(len(s.if_indexes) for s in port_map.sections) == 4
    # 物理パネルには物理ポートだけが並ぶ
    panel_indexes = [
        i for s in port_map.panels[0].sections for row in s.rows for i in row if i
    ]
    assert panel_indexes == ["1"]


def test_empty_interface_list():
    port_map = build_port_map([])
    assert port_map.panels == []
    assert port_map.sections == []


# --- 設定による上書き ---------------------------------------------------
def test_category_can_be_forced_by_if_index():
    interfaces = [iface(1, "GigabitEthernet1/0/1"), iface(2, "GigabitEthernet1/0/2")]
    layout = PortLayoutConfig(categories={"2": "stack"})
    port_map = build_port_map(interfaces, layout)

    assert port_map.categories["2"] == STACK
    assert [s.category for s in port_map.sections] == [STACK]


def test_category_can_be_forced_by_name():
    interfaces = [iface(1, "Port 1"), iface(2, "Port 2")]
    port_map = build_port_map(interfaces, PortLayoutConfig(categories={"Port 2": "vlan"}))
    assert port_map.categories["2"] == VLAN


def test_rows_can_be_forced():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 9)]
    port_map = build_port_map(interfaces, PortLayoutConfig(rows=1))
    assert len(port_map.panels[0].sections[0].rows) == 1


def test_sequential_order_skips_odd_top_layout():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 9)]
    port_map = build_port_map(interfaces, PortLayoutConfig(order="sequential"))
    rows = port_map.panels[0].sections[0].rows
    # 上段は先頭から 1 つおきではなく、並び順どおりに振り分けられる
    assert names(port_map, interfaces, rows[0]) == ["Port 1", "Port 3", "Port 5", "Port 7"]


def test_explicit_groups_split_the_panel():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 13)]
    layout = PortLayoutConfig(
        groups=[
            PortGroupConfig(name="本体", ports="1-8"),
            PortGroupConfig(name="増設", ports="9-12"),
        ]
    )
    port_map = build_port_map(interfaces, layout)

    assert [p.name for p in port_map.panels] == ["本体", "増設"]
    assert sum(len(row) for row in port_map.panels[0].sections[0].rows) == 8
    assert sum(len(row) for row in port_map.panels[1].sections[0].rows) == 4


def test_ports_outside_explicit_groups_are_still_shown():
    interfaces = [iface(n, f"Port {n}") for n in range(1, 13)]
    layout = PortLayoutConfig(groups=[PortGroupConfig(name="本体", ports="1-8")])
    port_map = build_port_map(interfaces, layout)

    shown = [i for p in port_map.panels for s in p.sections for row in s.rows for i in row if i]
    assert len(shown) == 12


def test_port_group_range_parsing():
    group = PortGroupConfig(name="g", ports="1-4, 9, 20-18")
    assert group.matches("1", 1)
    assert group.matches("4", 4)
    assert not group.matches("5", 5)
    assert group.matches("9", 9)
    assert group.matches("19", 19)  # 範囲が逆順でも解釈する


def test_port_group_falls_back_to_if_index():
    group = PortGroupConfig(name="g", ports="1-4")
    # 名前から番号が読めない場合は ifIndex で判定する
    assert group.matches("3", None)
    assert not group.matches("notanumber", None)


@pytest.mark.parametrize("spec", ["", "abc", "1-x", ","])
def test_invalid_port_range_is_rejected(spec):
    with pytest.raises(ValueError):
        PortGroupConfig(name="g", ports=spec)
