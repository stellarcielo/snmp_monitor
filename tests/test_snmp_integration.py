"""実際の SNMP エージェントを立てて通信する結合テスト。

pysnmp のコマンドレスポンダをローカルで動かし、SnmpClient が v2c / v3 の
それぞれで本当に GET と GETBULK を行えることを確認する。
"""

from __future__ import annotations

import socket

import pytest
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config as agent_config
from pysnmp.entity import engine as agent_engine
from pysnmp.entity.rfc3413 import cmdrsp, context

from snmp_monitor.config import DeviceConfig, SnmpV2cConfig, SnmpV3Config
from snmp_monitor.snmp.client import SnmpAuthError, SnmpClient, SnmpTimeoutError, decode_str

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYSTEM_GROUP = "1.3.6.1.2.1.1"

COMMUNITY = "test-community"
AUTH_KEY = "auth-key-12345"
PRIV_KEY = "priv-key-12345"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def agent():
    """SNMPv2c / v3 の両方を受け付けるローカルエージェント。

    テストと同じイベントループ上で動かす必要があるため async フィクスチャにしている。
    """
    port = free_port()
    engine = agent_engine.SnmpEngine()
    agent_config.add_transport(
        engine,
        udp.DOMAIN_NAME,
        udp.UdpTransport().open_server_mode(("127.0.0.1", port)),
    )
    agent_config.add_v1_system(engine, "area", COMMUNITY)
    agent_config.add_vacm_user(engine, 2, "area", "noAuthNoPriv", (1, 3, 6), (1, 3, 6))

    agent_config.add_v3_user(engine, "noauth-user")
    agent_config.add_vacm_user(engine, 3, "noauth-user", "noAuthNoPriv", (1, 3, 6), (1, 3, 6))

    agent_config.add_v3_user(
        engine, "auth-user", agent_config.USM_AUTH_HMAC96_SHA, AUTH_KEY
    )
    agent_config.add_vacm_user(engine, 3, "auth-user", "authNoPriv", (1, 3, 6), (1, 3, 6))

    agent_config.add_v3_user(
        engine,
        "priv-user",
        agent_config.USM_AUTH_HMAC192_SHA256,
        AUTH_KEY,
        agent_config.USM_PRIV_CFB256_AES,
        PRIV_KEY,
    )
    agent_config.add_vacm_user(engine, 3, "priv-user", "authPriv", (1, 3, 6), (1, 3, 6))

    snmp_context = context.SnmpContext(engine)
    cmdrsp.GetCommandResponder(engine, snmp_context)
    cmdrsp.NextCommandResponder(engine, snmp_context)
    cmdrsp.BulkCommandResponder(engine, snmp_context)

    engine.open_dispatcher()
    try:
        yield port
    finally:
        engine.close_dispatcher()


def make_client(port: int, snmp, timeout: float = 3.0, retries: int = 1) -> SnmpClient:
    return SnmpClient(
        DeviceConfig(
            id="local", host="127.0.0.1", port=port, snmp=snmp,
            timeout=timeout, retries=retries,
        )
    )


V2C = SnmpV2cConfig(community=COMMUNITY)
V3_NOAUTH = SnmpV3Config(username="noauth-user")
V3_AUTH = SnmpV3Config(username="auth-user", auth_protocol="sha", auth_key=AUTH_KEY)
V3_PRIV = SnmpV3Config(
    username="priv-user",
    auth_protocol="sha256",
    auth_key=AUTH_KEY,
    priv_protocol="aes256",
    priv_key=PRIV_KEY,
)


@pytest.mark.parametrize(
    ("snmp", "level"),
    [
        (V2C, "v2c"),
        (V3_NOAUTH, "noAuthNoPriv"),
        (V3_AUTH, "authNoPriv"),
        (V3_PRIV, "authPriv"),
    ],
)
async def test_get_works_for_every_security_level(agent, snmp, level):
    client = make_client(agent, snmp)
    try:
        values = await client.get({"descr": SYS_DESCR, "uptime": SYS_UPTIME})
    finally:
        await client.close()

    assert decode_str(values["descr"]), f"{level} で sysDescr を取得できませんでした"
    assert isinstance(values["uptime"], int)


@pytest.mark.parametrize("snmp", [V2C, V3_PRIV])
async def test_get_table_walks_with_getbulk(agent, snmp):
    client = make_client(agent, snmp)
    try:
        table = await client.get_table({"system": SYSTEM_GROUP})
    finally:
        await client.close()

    # system グループには sysDescr(1) から sysServices(7) 以上が並ぶ
    assert len(table) >= 7
    assert "1.0" in table
    assert decode_str(table["1.0"]["system"])


async def test_walk_returns_index_to_value_mapping(agent):
    client = make_client(agent, V2C)
    try:
        values = await client.walk(SYSTEM_GROUP)
    finally:
        await client.close()
    assert values["3.0"] is not None  # sysUpTime


async def test_wrong_community_times_out(agent):
    client = make_client(agent, SnmpV2cConfig(community="wrong"), timeout=0.5, retries=0)
    try:
        with pytest.raises(SnmpTimeoutError):
            await client.get({"descr": SYS_DESCR})
    finally:
        await client.close()


async def test_unknown_v3_user_is_reported_as_auth_error(agent):
    client = make_client(agent, SnmpV3Config(username="ghost-user"), timeout=0.5, retries=0)
    try:
        with pytest.raises((SnmpAuthError, SnmpTimeoutError)):
            await client.get({"descr": SYS_DESCR})
    finally:
        await client.close()


async def test_wrong_auth_key_is_rejected(agent):
    client = make_client(
        agent,
        SnmpV3Config(username="auth-user", auth_protocol="sha", auth_key="wrong-key-9999"),
        timeout=0.5,
        retries=0,
    )
    try:
        with pytest.raises((SnmpAuthError, SnmpTimeoutError)):
            await client.get({"descr": SYS_DESCR})
    finally:
        await client.close()


async def test_unreachable_host_times_out():
    # TEST-NET-1 (RFC 5737) は到達しないことが保証されている
    client = SnmpClient(
        DeviceConfig(
            id="dead", host="192.0.2.254", port=161, timeout=0.5, retries=0,
            snmp=SnmpV2cConfig(community="public"),
        )
    )
    try:
        with pytest.raises(SnmpTimeoutError):
            await client.get({"descr": SYS_DESCR})
    finally:
        await client.close()
