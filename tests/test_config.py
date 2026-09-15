"""設定ファイルの読み込みに関するテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from snmp_monitor.config import (
    AppConfig,
    DeviceConfig,
    SnmpV2cConfig,
    SnmpV3Config,
    load_config,
)


def write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_v2c_device_is_parsed(tmp_path):
    path = write(
        tmp_path,
        """
        devices:
          - id: sw1
            name: スイッチ
            host: 10.0.0.1
            snmp:
              version: "2c"
              community: secret
        """,
    )
    config = load_config(path)
    device = config.devices[0]
    assert isinstance(device.snmp, SnmpV2cConfig)
    assert device.snmp.community == "secret"
    assert device.display_name == "スイッチ"


def test_v3_device_and_security_level(tmp_path):
    path = write(
        tmp_path,
        """
        devices:
          - id: sw2
            host: 10.0.0.2
            snmp:
              version: "3"
              username: monitor
              auth_protocol: SHA
              auth_key: authkey123
              priv_protocol: AES
              priv_key: privkey123
        """,
    )
    device = load_config(path).devices[0]
    assert isinstance(device.snmp, SnmpV3Config)
    assert device.snmp.security_level == "authPriv"
    # 大文字で書いても正規化される
    assert device.snmp.auth_protocol == "sha"


def test_v3_noauth_level():
    snmp = SnmpV3Config(username="u")
    assert snmp.security_level == "noAuthNoPriv"


def test_v3_requires_auth_key():
    with pytest.raises(ValidationError):
        SnmpV3Config(username="u", auth_protocol="sha")


def test_v3_priv_requires_auth():
    with pytest.raises(ValidationError):
        SnmpV3Config(username="u", priv_protocol="aes", priv_key="k")


def test_environment_variables_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_COMMUNITY", "from-env")
    path = write(
        tmp_path,
        """
        devices:
          - id: sw3
            host: 10.0.0.3
            snmp:
              version: "2c"
              community: ${TEST_COMMUNITY}
          - id: sw4
            host: 10.0.0.4
            snmp:
              version: "2c"
              community: ${MISSING_VAR:-fallback}
        """,
    )
    config = load_config(path)
    assert config.devices[0].snmp.community == "from-env"
    assert config.devices[1].snmp.community == "fallback"


def test_missing_environment_variable_raises(tmp_path):
    path = write(
        tmp_path,
        """
        devices:
          - id: sw5
            host: 10.0.0.5
            snmp: { version: "2c", community: "${DEFINITELY_NOT_SET_12345}" }
        """,
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_duplicate_device_id_is_rejected():
    with pytest.raises(ValidationError):
        AppConfig(
            devices=[
                DeviceConfig(id="a", host="1.1.1.1"),
                DeviceConfig(id="a", host="2.2.2.2"),
            ]
        )


def test_invalid_device_id_is_rejected():
    with pytest.raises(ValidationError):
        DeviceConfig(id="bad id!", host="1.1.1.1")


def test_enabled_devices_filter():
    config = AppConfig(
        devices=[
            DeviceConfig(id="a", host="1.1.1.1"),
            DeviceConfig(id="b", host="2.2.2.2", enabled=False),
        ]
    )
    assert [d.id for d in config.enabled_devices] == ["a"]
    assert config.device("b") is not None
    assert config.device("zzz") is None


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_example_config_is_valid(monkeypatch):
    """同梱のサンプル設定が、そのまま読み込める状態を保っていること。"""
    for name in ("SNMP_COMMUNITY", "SNMP_AUTH_KEY", "SNMP_PRIV_KEY"):
        monkeypatch.setenv(name, "dummy-value")

    config = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")

    assert config.devices
    layout = config.device("switch-2f").port_layout
    assert layout is not None
    assert [g.name for g in layout.groups] == ["本体", "SFP"]
    assert layout.categories["49"] == "stack"
