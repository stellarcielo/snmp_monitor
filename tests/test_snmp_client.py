"""SNMP クライアントの値変換まわりのテスト。"""

from __future__ import annotations

import pytest
from pysnmp.proto import errind
from pysnmp.proto.rfc1902 import Counter64, Integer32, OctetString, TimeTicks
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

from snmp_monitor.config import DeviceConfig, SnmpV2cConfig, SnmpV3Config
from snmp_monitor.snmp.client import (
    SnmpAuthError,
    SnmpClient,
    SnmpError,
    SnmpTimeoutError,
    _classify,
    _oid_suffix,
    decode_str,
    format_mac,
    to_python,
)


def test_integers_are_converted():
    assert to_python(Integer32(42)) == 42
    assert to_python(Counter64(2**40)) == 2**40
    assert to_python(TimeTicks(12345)) == 12345


def test_octet_string_stays_bytes():
    assert to_python(OctetString("hello")) == b"hello"


def test_missing_values_become_none():
    assert to_python(NoSuchObject()) is None
    assert to_python(NoSuchInstance()) is None
    assert to_python(None) is None


def test_decode_str_strips_control_characters():
    assert decode_str(b"Switch-01\x00") == "Switch-01"
    assert decode_str(b"  spaced  ") == "spaced"
    assert decode_str(b"") is None
    assert decode_str(None) is None


def test_decode_str_handles_invalid_utf8():
    assert decode_str(b"\xff\xfeabc") is not None


def test_format_mac():
    assert format_mac(b"\x00\x11\x22\xaa\xbb\xcc") == "00:11:22:aa:bb:cc"
    assert format_mac(b"") is None
    assert format_mac("not bytes") is None


def test_oid_suffix():
    assert _oid_suffix("1.3.6.1.2.1.2.2.1.2.5", "1.3.6.1.2.1.2.2.1.2") == "5"
    assert _oid_suffix("1.3.6.1.2.1.2.2.1.2", "1.3.6.1.2.1.2.2.1.2") == ""
    # 別のカラムに入った場合は None (走査の打ち切り判定に使う)
    assert _oid_suffix("1.3.6.1.2.1.2.2.1.3.5", "1.3.6.1.2.1.2.2.1.2") is None


def test_error_classification_by_indication_type():
    assert isinstance(_classify(errind.requestTimedOut), SnmpTimeoutError)
    assert isinstance(_classify(errind.emptyResponse), SnmpTimeoutError)
    assert isinstance(_classify(errind.wrongDigest), SnmpAuthError)
    assert isinstance(_classify(errind.unknownUserName), SnmpAuthError)
    assert isinstance(_classify(errind.notInTimeWindow), SnmpAuthError)
    assert isinstance(_classify(errind.tooBig), SnmpError)


def test_missing_crypto_library_is_explained():
    error = _classify(errind.decryptionError)
    assert isinstance(error, SnmpAuthError)
    assert "cryptography" in str(error)


def test_error_classification_falls_back_to_text():
    assert isinstance(_classify("request timed out"), SnmpTimeoutError)
    assert isinstance(_classify("bad digest"), SnmpAuthError)


@pytest.mark.parametrize(
    "snmp",
    [
        SnmpV2cConfig(community="public"),
        SnmpV3Config(username="u"),
        SnmpV3Config(username="u", auth_protocol="sha", auth_key="k" * 8),
        SnmpV3Config(
            username="u",
            auth_protocol="sha256",
            auth_key="k" * 8,
            priv_protocol="aes256",
            priv_key="p" * 8,
        ),
    ],
)
async def test_client_builds_credentials_for_all_versions(snmp):
    device = DeviceConfig(id="d", host="127.0.0.1", snmp=snmp)
    client = SnmpClient(device)
    try:
        assert client._auth is not None
    finally:
        await client.close()


async def test_get_with_no_oids_returns_empty():
    client = SnmpClient(DeviceConfig(id="d", host="127.0.0.1"))
    try:
        assert await client.get([]) == {}
        assert await client.get_table({}) == {}
    finally:
        await client.close()
