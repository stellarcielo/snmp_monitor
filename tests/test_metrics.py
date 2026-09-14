"""レート計算のテスト。"""

from __future__ import annotations

from snmp_monitor.metrics import (
    COUNTER32_MAX,
    COUNTER64_MAX,
    RateCalculator,
    max_plausible_rate,
    octets_to_bps,
    speed_to_bps,
)


def test_first_sample_returns_none():
    calc = RateCalculator()
    assert calc.update("k", 100, 0.0) is None


def test_rate_is_delta_per_second():
    calc = RateCalculator()
    calc.update("k", 1000, 0.0)
    assert calc.update("k", 1600, 10.0) == 60.0


def test_none_value_is_ignored():
    calc = RateCalculator()
    assert calc.update("k", None, 0.0) is None
    assert len(calc) == 0


def test_counter32_wrap_is_corrected():
    calc = RateCalculator()
    calc.update("k", COUNTER32_MAX - 100, 0.0)
    # 100 で折り返し、さらに 50 進んだので 150 オクテット / 10 秒
    assert calc.update("k", 50, 10.0, bits=32) == 15.0


def test_counter64_wrap_is_corrected():
    calc = RateCalculator()
    calc.update("k", COUNTER64_MAX - 10, 0.0)
    assert calc.update("k", 10, 10.0, bits=64) == 2.0


def test_time_going_backwards_returns_none():
    calc = RateCalculator()
    calc.update("k", 100, 100.0)
    assert calc.update("k", 200, 50.0) is None


def test_stale_sample_is_discarded():
    calc = RateCalculator(max_age_seconds=60)
    calc.update("k", 100, 0.0)
    assert calc.update("k", 200, 3600.0) is None


def test_rate_above_link_speed_is_discarded():
    calc = RateCalculator()
    calc.update("k", 0, 0.0)
    # 1Gbps の回線で 100Gbps 相当の値は異常とみなす
    limit = max_plausible_rate(1_000_000_000)
    assert calc.update("k", 10**12, 1.0, max_rate=limit) is None


def test_forget_prefix_clears_only_matching_keys():
    calc = RateCalculator()
    calc.update("dev1:in", 1, 0.0)
    calc.update("dev1:out", 1, 0.0)
    calc.update("dev2:in", 1, 0.0)
    calc.forget_prefix("dev1:")
    assert len(calc) == 1
    assert calc.update("dev2:in", 11, 10.0) == 1.0


def test_octets_to_bps():
    assert octets_to_bps(125_000_000) == 1_000_000_000
    assert octets_to_bps(None) is None


def test_speed_prefers_high_speed():
    # ifSpeed は 32bit で頭打ちになるため ifHighSpeed (Mbps) を優先する
    assert speed_to_bps(4_294_967_295, 10_000) == 10_000_000_000
    assert speed_to_bps(1_000_000_000, None) == 1_000_000_000
    assert speed_to_bps(None, None) is None


def test_max_plausible_rate_defaults_when_speed_unknown():
    assert max_plausible_rate(None) > max_plausible_rate(1_000_000_000)
