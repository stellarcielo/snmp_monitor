"""カウンタ値からレート (bps 等) を算出するためのユーティリティ。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

COUNTER32_MAX = 2**32
COUNTER64_MAX = 2**64


@dataclass(slots=True)
class _Sample:
    value: int
    ts: float


class RateCalculator:
    """単調増加カウンタの差分から 1 秒あたりのレートを求める。

    - カウンタの折り返し (wrap) を検出して補正する
    - 前回値が古すぎる場合は基準を貼り替えるだけで値は返さない
    - ``max_rate`` を超える不自然な値は捨てる (複数回 wrap・カウンタリセット対策)
    """

    def __init__(self, max_age_seconds: float = 900.0) -> None:
        self._samples: dict[str, _Sample] = {}
        self._max_age = max_age_seconds

    def update(
        self,
        key: str,
        value: int | None,
        ts: float,
        *,
        bits: int = 64,
        max_rate: float | None = None,
    ) -> float | None:
        """カウンタ値を投入し、前回との差分からレートを返す。

        初回・時刻の逆行・値が信用できない場合は ``None`` を返す。
        """
        if value is None:
            return None
        value = int(value)
        previous = self._samples.get(key)
        self._samples[key] = _Sample(value, ts)

        if previous is None:
            return None
        delta_t = ts - previous.ts
        if delta_t <= 0:
            return None
        if delta_t > self._max_age:
            # 間隔が空きすぎた区間はレートとして意味がないので捨てる
            return None

        delta = value - previous.value
        if delta < 0:
            # wrap または機器側のカウンタリセット
            delta += COUNTER32_MAX if bits == 32 else COUNTER64_MAX
            if delta < 0:
                return None

        rate = delta / delta_t
        if max_rate is not None and rate > max_rate:
            logger.debug(
                "レートが上限を超えたため破棄しました key=%s rate=%.1f max=%.1f",
                key,
                rate,
                max_rate,
            )
            return None
        return rate

    def forget(self, key: str) -> None:
        self._samples.pop(key, None)

    def forget_prefix(self, prefix: str) -> None:
        """デバイス再起動時などに、そのデバイス分の基準値をまとめて破棄する。"""
        for key in [k for k in self._samples if k.startswith(prefix)]:
            del self._samples[key]

    def __len__(self) -> int:
        return len(self._samples)


def octets_to_bps(rate: float | None) -> float | None:
    """オクテット/秒 を bps に変換する。"""
    return None if rate is None else rate * 8.0


def speed_to_bps(if_speed: int | None, if_high_speed: int | None) -> int | None:
    """ifSpeed / ifHighSpeed からリンク速度 (bps) を決める。

    ifSpeed は Gauge32 のため 4.294Gbps で頭打ちになる。
    その場合は Mbps 単位の ifHighSpeed を優先する。
    """
    if if_high_speed:
        return int(if_high_speed) * 1_000_000
    if if_speed:
        return int(if_speed)
    return None


def wrap_bits_for(counter_is_64bit: bool) -> int:
    return 64 if counter_is_64bit else 32


def max_plausible_rate(speed_bps: int | None) -> float | None:
    """リンク速度から、ありえるオクテット/秒 の上限を求める。

    リンク速度が不明な場合は 100Gbps 相当を上限とする。
    多少の誤差を許容するため 1.5 倍の余裕を持たせている。
    """
    limit_bps = speed_bps if speed_bps and speed_bps > 0 else 100_000_000_000
    return limit_bps * 1.5 / 8.0
