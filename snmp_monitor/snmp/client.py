"""pysnmp を asyncio で扱うための薄いラッパ。

SNMP v2c / v3 のどちらにも対応し、GET と GETBULK によるテーブル走査を提供する。
MIB ファイルへの依存を避けるため、数値 OID のみを扱う (``lookupMib=False``)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_cmd,
    get_cmd,
    usm3DESEDEPrivProtocol,
    usmAesCfb128Protocol,
    usmAesCfb192Protocol,
    usmAesCfb256Protocol,
    usmDESPrivProtocol,
    usmHMAC128SHA224AuthProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMAC256SHA384AuthProtocol,
    usmHMAC384SHA512AuthProtocol,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)
from pysnmp.proto import errind
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from ..config import DeviceConfig, SnmpV2cConfig, SnmpV3Config

logger = logging.getLogger(__name__)

AUTH_PROTOCOLS = {
    "none": usmNoAuthProtocol,
    "md5": usmHMACMD5AuthProtocol,
    "sha": usmHMACSHAAuthProtocol,
    "sha224": usmHMAC128SHA224AuthProtocol,
    "sha256": usmHMAC192SHA256AuthProtocol,
    "sha384": usmHMAC256SHA384AuthProtocol,
    "sha512": usmHMAC384SHA512AuthProtocol,
}

PRIV_PROTOCOLS = {
    "none": usmNoPrivProtocol,
    "des": usmDESPrivProtocol,
    "3des": usm3DESEDEPrivProtocol,
    "aes": usmAesCfb128Protocol,
    "aes128": usmAesCfb128Protocol,
    "aes192": usmAesCfb192Protocol,
    "aes256": usmAesCfb256Protocol,
}

#: 応答が「その OID は存在しない」ことを示す型
_NULL_TYPES = (NoSuchObject, NoSuchInstance, EndOfMibView)


class SnmpError(Exception):
    """SNMP 通信に関する基底例外。"""


class SnmpTimeoutError(SnmpError):
    """応答が返らなかった (デバイス停止・到達不可・community 不一致など)。"""


class SnmpAuthError(SnmpError):
    """SNMPv3 の認証・暗号化に失敗した。"""


#: 応答が得られなかったことを示す errorIndication
_TIMEOUT_INDICATIONS = (errind.RequestTimedOut, errind.EmptyResponse)

#: 認証・暗号・アクセス制御の失敗を示す errorIndication
_AUTH_INDICATIONS = (
    errind.UnknownCommunityName,
    errind.NoEncryption,
    errind.EncryptionError,
    errind.DecryptionError,
    errind.NoAuthentication,
    errind.AuthenticationError,
    errind.AuthenticationFailure,
    errind.UnsupportedAuthProtocol,
    errind.UnsupportedPrivProtocol,
    errind.UnknownSecurityName,
    errind.UnsupportedSecurityModel,
    errind.UnsupportedSecurityLevel,
    errind.NotInTimeWindow,
    errind.UnknownUserName,
    errind.WrongDigest,
    errind.UnknownEngineID,
)

#: 型で判定できない場合に使う文字列のヒント
_TIMEOUT_HINTS = ("timed out", "timeout", "no snmp response")
_AUTH_HINTS = ("digest", "username", "securitylevel", "securityname", "decryption")

_CRYPTO_HINT = (
    " (SNMPv3 の暗号化には cryptography パッケージが必要です: pip install cryptography)"
)


def _classify(error_indication: Any) -> SnmpError:
    """pysnmp の errorIndication を、扱いやすい例外に振り分ける。"""
    text = str(error_indication)
    if isinstance(error_indication, _TIMEOUT_INDICATIONS):
        return SnmpTimeoutError(text)
    if isinstance(error_indication, _AUTH_INDICATIONS):
        # 暗号ライブラリ未導入のときも同じ indication になるため補足を添える
        if isinstance(
            error_indication, (errind.EncryptionError, errind.DecryptionError)
        ):
            return SnmpAuthError(text + _CRYPTO_HINT)
        return SnmpAuthError(text)

    lowered = text.lower().replace(" ", "")
    if any(h.replace(" ", "") in lowered for h in _TIMEOUT_HINTS):
        return SnmpTimeoutError(text)
    if any(h in lowered for h in _AUTH_HINTS):
        return SnmpAuthError(text)
    return SnmpError(text)


def to_python(value: Any) -> int | bytes | str | None:
    """pysnmp の値を Python のプリミティブへ変換する。

    OctetString はエンコーディングが不定なため ``bytes`` のまま返し、
    文字列化は :func:`decode_str`、MAC は :func:`format_mac` で行う。
    """
    if value is None or isinstance(value, _NULL_TYPES):
        return None
    # Counter64 などの派生型も含めて整数として扱う
    if hasattr(value, "asOctets") and not isinstance(value, ObjectIdentity):
        try:
            return bytes(value.asOctets())
        except Exception:  # noqa: BLE001 - 変換不能なら prettyPrint に退避
            return str(value.prettyPrint())
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value.prettyPrint()) if hasattr(value, "prettyPrint") else str(value)


def decode_str(value: Any) -> str | None:
    """DisplayString 相当の値を文字列にする。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    # 機器によっては末尾に NUL や制御文字が付くため取り除く
    return text.replace("\x00", "").strip() or None


def format_mac(value: Any) -> str | None:
    """ifPhysAddress を ``aa:bb:cc:dd:ee:ff`` 形式にする。"""
    if not isinstance(value, bytes) or not value:
        return None
    return ":".join(f"{b:02x}" for b in value)


def _oid_suffix(oid: str, base: str) -> str | None:
    """``base`` 配下の OID からインデックス部分を取り出す。"""
    if oid == base:
        return ""
    prefix = base + "."
    return oid[len(prefix) :] if oid.startswith(prefix) else None


class SnmpClient:
    """デバイス 1 台分の SNMP セッション。"""

    def __init__(self, device: DeviceConfig) -> None:
        self.device = device
        self._engine = SnmpEngine()
        self._auth = self._build_auth(device)
        self._context = self._build_context(device)
        self._target: UdpTransportTarget | None = None
        self._target_lock = asyncio.Lock()
        self._closed = False

    # -- セットアップ ----------------------------------------------------
    @staticmethod
    def _build_auth(device: DeviceConfig) -> CommunityData | UsmUserData:
        snmp = device.snmp
        if isinstance(snmp, SnmpV2cConfig):
            # mpModel=1 が SNMPv2c
            return CommunityData(snmp.community, mpModel=1)
        if isinstance(snmp, SnmpV3Config):
            return UsmUserData(
                snmp.username,
                authKey=snmp.auth_key,
                privKey=snmp.priv_key,
                authProtocol=AUTH_PROTOCOLS[snmp.auth_protocol],
                privProtocol=PRIV_PROTOCOLS[snmp.priv_protocol],
            )
        raise SnmpError(f"未対応の SNMP 設定です: {snmp!r}")

    @staticmethod
    def _build_context(device: DeviceConfig) -> ContextData:
        snmp = device.snmp
        if isinstance(snmp, SnmpV3Config) and snmp.context_name:
            return ContextData(contextName=snmp.context_name)
        return ContextData()

    async def _get_target(self) -> UdpTransportTarget:
        if self._target is None:
            async with self._target_lock:
                if self._target is None:
                    self._target = await UdpTransportTarget.create(
                        (self.device.host, self.device.port),
                        timeout=self.device.timeout,
                        retries=self.device.retries,
                    )
        return self._target

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._engine.close_dispatcher()
        except Exception:  # noqa: BLE001 - 後始末の失敗は致命的ではない
            logger.debug("dispatcher の終了に失敗しました", exc_info=True)

    async def __aenter__(self) -> SnmpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- SNMP 操作 -------------------------------------------------------
    async def get(self, oids: Mapping[str, str] | Sequence[str]) -> dict[str, Any]:
        """GET を 1 回発行する。

        ``oids`` がマッピングなら ``{名前: 値}``、シーケンスなら ``{OID: 値}`` を返す。
        """
        names: list[str]
        targets: list[str]
        if isinstance(oids, Mapping):
            names, targets = list(oids.keys()), list(oids.values())
        else:
            names = targets = list(oids)
        if not targets:
            return {}

        target = await self._get_target()
        error_indication, error_status, error_index, var_binds = await get_cmd(
            self._engine,
            self._auth,
            target,
            self._context,
            *(ObjectType(ObjectIdentity(o)) for o in targets),
            lookupMib=False,
        )
        if error_indication:
            raise _classify(error_indication)
        if error_status:
            raise SnmpError(
                f"{error_status.prettyPrint()} (at index {int(error_index or 0)})"
            )
        return {names[i]: to_python(vb[1]) for i, vb in enumerate(var_binds) if i < len(names)}

    async def get_table(
        self,
        columns: Mapping[str, str],
        *,
        max_repetitions: int = 25,
        max_rows: int = 4096,
    ) -> dict[str, dict[str, Any]]:
        """テーブルを GETBULK で走査し ``{インデックス: {列名: 値}}`` を返す。

        複数列を 1 リクエストにまとめるため、往復回数は列数によらずほぼ一定になる。
        """
        if not columns:
            return {}

        names = list(columns)
        bases = {name: columns[name] for name in names}
        cursors = dict(bases)
        active = set(names)
        rows: dict[str, dict[str, Any]] = {}
        target = await self._get_target()

        while active:
            ordered = [n for n in names if n in active]
            error_indication, error_status, error_index, var_binds = await bulk_cmd(
                self._engine,
                self._auth,
                target,
                self._context,
                0,
                max_repetitions,
                *(ObjectType(ObjectIdentity(cursors[n])) for n in ordered),
                lookupMib=False,
            )
            if error_indication:
                raise _classify(error_indication)
            if error_status:
                raise SnmpError(
                    f"{error_status.prettyPrint()} (at index {int(error_index or 0)})"
                )
            if not var_binds:
                break

            progressed = False
            for position, var_bind in enumerate(var_binds):
                column = ordered[position % len(ordered)]
                if column not in active:
                    continue
                oid_obj, value = var_bind
                oid = str(oid_obj)
                index = _oid_suffix(oid, bases[column])
                if index is None or index == "" or isinstance(value, _NULL_TYPES):
                    # テーブルの範囲外に出た列はここで打ち切る
                    active.discard(column)
                    continue
                cursors[column] = oid
                progressed = True
                rows.setdefault(index, {})[column] = to_python(value)

            if not progressed:
                break
            if len(rows) >= max_rows:
                logger.warning(
                    "%s: テーブル行数が上限 %d に達したため走査を打ち切ります",
                    self.device.id,
                    max_rows,
                )
                break

        return rows

    async def walk(self, base_oid: str, **kwargs: Any) -> dict[str, Any]:
        """単一カラム配下を走査し ``{インデックス: 値}`` を返す。"""
        table = await self.get_table({"value": base_oid}, **kwargs)
        return {index: row.get("value") for index, row in table.items()}
