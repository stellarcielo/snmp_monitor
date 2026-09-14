"""設定ファイル (YAML) の読み込みとバリデーション。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# ${ENV_VAR} / ${ENV_VAR:-default} を環境変数で置換するためのパターン
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ValueError(f"環境変数 {name} が未定義です (設定ファイル内で参照されています)")

    return _ENV_PATTERN.sub(repl, value)


def _expand_tree(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _expand_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v) for v in node]
    if isinstance(node, str):
        return _expand_env(node)
    return node


class SnmpV2cConfig(BaseModel):
    """SNMP v2c (コミュニティ文字列) の認証設定。"""

    version: Literal["2c"] = "2c"
    community: str = "public"


AuthProtocol = Literal["none", "md5", "sha", "sha224", "sha256", "sha384", "sha512"]
PrivProtocol = Literal["none", "des", "3des", "aes", "aes128", "aes192", "aes256"]


class SnmpV3Config(BaseModel):
    """SNMP v3 (USM) の認証設定。"""

    version: Literal["3"] = "3"
    username: str
    auth_protocol: AuthProtocol = "none"
    auth_key: str | None = None
    priv_protocol: PrivProtocol = "none"
    priv_key: str | None = None
    context_name: str = ""
    engine_id: str | None = None

    @field_validator("auth_protocol", "priv_protocol", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_keys(self) -> SnmpV3Config:
        if self.auth_protocol != "none" and not self.auth_key:
            raise ValueError("auth_protocol を指定した場合は auth_key が必須です")
        if self.priv_protocol != "none":
            if not self.priv_key:
                raise ValueError("priv_protocol を指定した場合は priv_key が必須です")
            if self.auth_protocol == "none":
                raise ValueError("暗号化 (priv) を使う場合は認証 (auth) も必須です")
        return self

    @property
    def security_level(self) -> str:
        if self.auth_protocol == "none":
            return "noAuthNoPriv"
        if self.priv_protocol == "none":
            return "authNoPriv"
        return "authPriv"


SnmpAuthConfig = SnmpV2cConfig | SnmpV3Config


class DeviceConfig(BaseModel):
    """監視対象デバイス 1 台分の設定。"""

    id: str
    name: str | None = None
    host: str
    port: int = 161
    snmp: SnmpAuthConfig = Field(default_factory=SnmpV2cConfig, discriminator="version")
    timeout: float = 3.0
    retries: int = 1
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    # 監視対象インターフェースの絞り込み (ifName / ifDescr の正規表現)
    interface_include: str | None = None
    interface_exclude: str | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", v):
            raise ValueError("device.id には英数字と . _ : - のみ使用できます")
        return v

    @property
    def display_name(self) -> str:
        return self.name or self.host


class PollingConfig(BaseModel):
    """ポーリング動作の設定。"""

    interval_seconds: float = Field(default=30.0, ge=5.0)
    max_concurrent_devices: int = Field(default=16, ge=1)
    # システムリソース (CPU/メモリ/ディスク) を何回に 1 回取得するか
    resource_every_n_polls: int = Field(default=1, ge=1)
    # インターフェースの構成情報 (名前/速度など) を何回に 1 回取り直すか
    inventory_every_n_polls: int = Field(default=10, ge=1)


class StorageConfig(BaseModel):
    """SQLite 保存の設定。"""

    path: Path = Path("data/snmp_monitor.db")
    raw_retention_hours: int = Field(default=48, ge=1)
    rollup_5m_retention_days: int = Field(default=30, ge=1)
    rollup_1h_retention_days: int = Field(default=400, ge=1)
    rollup_interval_seconds: float = Field(default=300.0, ge=60.0)


class ServerConfig(BaseModel):
    """Web サーバの設定。"""

    host: str = "127.0.0.1"
    port: int = 8080


class AppConfig(BaseModel):
    """アプリ全体の設定。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    devices: list[DeviceConfig] = Field(default_factory=list)
    # 実機なしで UI を確認するためのデモモード
    demo_mode: bool = False

    @model_validator(mode="after")
    def _unique_device_ids(self) -> AppConfig:
        seen: set[str] = set()
        for d in self.devices:
            if d.id in seen:
                raise ValueError(f"device.id が重複しています: {d.id}")
            seen.add(d.id)
        return self

    def device(self, device_id: str) -> DeviceConfig | None:
        return next((d for d in self.devices if d.id == device_id), None)

    @property
    def enabled_devices(self) -> list[DeviceConfig]:
        return [d for d in self.devices if d.enabled]


def load_config(path: str | Path) -> AppConfig:
    """YAML 設定ファイルを読み込む。`${VAR}` 形式で環境変数を展開する。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("設定ファイルのトップレベルはマッピングである必要があります")
    return AppConfig.model_validate(_expand_tree(raw))
