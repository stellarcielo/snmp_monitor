"""監視で使用する OID の定義。

数値 OID を直接扱うことで MIB コンパイル済みファイルへの依存をなくしている。
"""

from __future__ import annotations

# --- SNMPv2-MIB (system グループ) ---------------------------------------
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"  # TimeTicks (1/100 秒)
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

SYSTEM_OIDS = {
    "sys_descr": SYS_DESCR,
    "sys_object_id": SYS_OBJECT_ID,
    "uptime_ticks": SYS_UPTIME,
    "sys_contact": SYS_CONTACT,
    "sys_name": SYS_NAME,
    "sys_location": SYS_LOCATION,
}

# --- IF-MIB: ifTable (1.3.6.1.2.1.2.2.1) --------------------------------
IF_TABLE = "1.3.6.1.2.1.2.2.1"
IF_INDEX = f"{IF_TABLE}.1"
IF_DESCR = f"{IF_TABLE}.2"
IF_TYPE = f"{IF_TABLE}.3"
IF_MTU = f"{IF_TABLE}.4"
IF_SPEED = f"{IF_TABLE}.5"  # bps (Gauge32, 最大 4294967295)
IF_PHYS_ADDRESS = f"{IF_TABLE}.6"
IF_ADMIN_STATUS = f"{IF_TABLE}.7"
IF_OPER_STATUS = f"{IF_TABLE}.8"
IF_LAST_CHANGE = f"{IF_TABLE}.9"
IF_IN_OCTETS = f"{IF_TABLE}.10"  # Counter32
IF_IN_UCAST_PKTS = f"{IF_TABLE}.11"
IF_IN_DISCARDS = f"{IF_TABLE}.13"
IF_IN_ERRORS = f"{IF_TABLE}.14"
IF_OUT_OCTETS = f"{IF_TABLE}.16"  # Counter32
IF_OUT_UCAST_PKTS = f"{IF_TABLE}.17"
IF_OUT_DISCARDS = f"{IF_TABLE}.19"
IF_OUT_ERRORS = f"{IF_TABLE}.20"

# --- IF-MIB: ifXTable (1.3.6.1.2.1.31.1.1.1) ----------------------------
IFX_TABLE = "1.3.6.1.2.1.31.1.1.1"
IF_NAME = f"{IFX_TABLE}.1"
IF_IN_MULTICAST_PKTS = f"{IFX_TABLE}.2"
IF_IN_BROADCAST_PKTS = f"{IFX_TABLE}.3"
IF_HC_IN_OCTETS = f"{IFX_TABLE}.6"  # Counter64
IF_HC_IN_UCAST_PKTS = f"{IFX_TABLE}.7"
IF_HC_OUT_OCTETS = f"{IFX_TABLE}.10"  # Counter64
IF_HC_OUT_UCAST_PKTS = f"{IFX_TABLE}.11"
IF_HIGH_SPEED = f"{IFX_TABLE}.15"  # Mbps
IF_ALIAS = f"{IFX_TABLE}.18"

#: インターフェースの構成情報 (毎回は取得しない)
INTERFACE_INVENTORY_COLUMNS = {
    "descr": IF_DESCR,
    "if_type": IF_TYPE,
    "mtu": IF_MTU,
    "speed": IF_SPEED,
    "phys_address": IF_PHYS_ADDRESS,
    "admin_status": IF_ADMIN_STATUS,
    "last_change": IF_LAST_CHANGE,
}

INTERFACE_INVENTORY_COLUMNS_X = {
    "name": IF_NAME,
    "high_speed": IF_HIGH_SPEED,
    "alias": IF_ALIAS,
}

#: 毎ポーリングで取得するカウンタ類 (ifTable)
INTERFACE_COUNTER_COLUMNS = {
    "oper_status": IF_OPER_STATUS,
    "in_octets_32": IF_IN_OCTETS,
    "out_octets_32": IF_OUT_OCTETS,
    "in_errors": IF_IN_ERRORS,
    "out_errors": IF_OUT_ERRORS,
    "in_discards": IF_IN_DISCARDS,
    "out_discards": IF_OUT_DISCARDS,
}

#: 毎ポーリングで取得するカウンタ類 (ifXTable / 64bit)
INTERFACE_COUNTER_COLUMNS_X = {
    "in_octets_64": IF_HC_IN_OCTETS,
    "out_octets_64": IF_HC_OUT_OCTETS,
}

# --- HOST-RESOURCES-MIB -------------------------------------------------
HR_SYSTEM_UPTIME = "1.3.6.1.2.1.25.1.1.0"
HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"  # コアごとの負荷率 (%)

HR_STORAGE_TABLE = "1.3.6.1.2.1.25.2.3.1"
HR_STORAGE_INDEX = f"{HR_STORAGE_TABLE}.1"
HR_STORAGE_TYPE = f"{HR_STORAGE_TABLE}.2"
HR_STORAGE_DESCR = f"{HR_STORAGE_TABLE}.3"
HR_STORAGE_ALLOCATION_UNITS = f"{HR_STORAGE_TABLE}.4"
HR_STORAGE_SIZE = f"{HR_STORAGE_TABLE}.5"
HR_STORAGE_USED = f"{HR_STORAGE_TABLE}.6"

HR_STORAGE_COLUMNS = {
    "storage_type": HR_STORAGE_TYPE,
    "descr": HR_STORAGE_DESCR,
    "allocation_units": HR_STORAGE_ALLOCATION_UNITS,
    "size": HR_STORAGE_SIZE,
    "used": HR_STORAGE_USED,
}

# hrStorageType の値 (hrStorageTypes 配下)
HR_STORAGE_TYPE_RAM = "1.3.6.1.2.1.25.2.1.2"
HR_STORAGE_TYPE_VIRTUAL_MEMORY = "1.3.6.1.2.1.25.2.1.3"
HR_STORAGE_TYPE_FIXED_DISK = "1.3.6.1.2.1.25.2.1.4"
HR_STORAGE_TYPE_OTHER = "1.3.6.1.2.1.25.2.1.1"

# --- UCD-SNMP-MIB (HOST-RESOURCES が無い機器向けのフォールバック) -------
UCD_SS_CPU_IDLE = "1.3.6.1.4.1.2021.11.11.0"  # % (瞬時値)
UCD_SS_CPU_USER = "1.3.6.1.4.1.2021.11.9.0"
UCD_SS_CPU_SYSTEM = "1.3.6.1.4.1.2021.11.10.0"
UCD_LA_LOAD_1 = "1.3.6.1.4.1.2021.10.1.3.1"  # 文字列表現のロードアベレージ
UCD_MEM_TOTAL_REAL = "1.3.6.1.4.1.2021.4.5.0"  # KB
UCD_MEM_AVAIL_REAL = "1.3.6.1.4.1.2021.4.6.0"  # KB
UCD_MEM_BUFFER = "1.3.6.1.4.1.2021.4.14.0"  # KB
UCD_MEM_CACHED = "1.3.6.1.4.1.2021.4.15.0"  # KB

UCD_DISK_TABLE = "1.3.6.1.4.1.2021.9.1"
UCD_DISK_PATH = f"{UCD_DISK_TABLE}.2"
UCD_DISK_TOTAL = f"{UCD_DISK_TABLE}.6"  # KB
UCD_DISK_USED = f"{UCD_DISK_TABLE}.8"  # KB

UCD_DISK_COLUMNS = {
    "path": UCD_DISK_PATH,
    "total_kb": UCD_DISK_TOTAL,
    "used_kb": UCD_DISK_USED,
}

UCD_CPU_MEM_OIDS = {
    "cpu_idle": UCD_SS_CPU_IDLE,
    "mem_total_kb": UCD_MEM_TOTAL_REAL,
    "mem_avail_kb": UCD_MEM_AVAIL_REAL,
    "mem_buffer_kb": UCD_MEM_BUFFER,
    "mem_cached_kb": UCD_MEM_CACHED,
}

# --- 値のラベル ---------------------------------------------------------
IF_STATUS_LABELS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

#: 代表的な ifType (IANAifType-MIB) のラベル
IF_TYPE_LABELS = {
    1: "other",
    6: "ethernet",
    24: "loopback",
    53: "propVirtual",
    71: "wireless",
    131: "tunnel",
    135: "l2vlan",
    136: "l3vlan",
    161: "lag",
}


def status_label(value: int | None) -> str:
    """ifOperStatus / ifAdminStatus の数値をラベルに変換する。"""
    if value is None:
        return "unknown"
    return IF_STATUS_LABELS.get(int(value), f"unknown({value})")


def if_type_label(value: int | None) -> str:
    if value is None:
        return "unknown"
    return IF_TYPE_LABELS.get(int(value), f"type{value}")
