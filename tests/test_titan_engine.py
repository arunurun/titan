import math

import pandas as pd
import pytest

from titan_engine import (
    calculate_adx,
    calculate_atr,
    calculate_atr_ratio,
    calculate_absorption_ratio,
    calculate_breakout_20d_distances_pct,
    calculate_cmf,
    calculate_ema,
    calculate_equity_technical_score,
    calculate_intent_score,
    calculate_obv_slope,
    calculate_z_score,
    find_call_put_oi_walls,
    find_oi_walls,
    get_pcr,
)


def test_z_score_flat_is_zero():
    s = pd.Series([100.0] * 30)
    z = calculate_z_score(s, window=20)
    assert z == 0.0


def test_z_score_spike_high():
    base = [100.0] * 25
    base.append(130.0)
    s = pd.Series(base)
    z = calculate_z_score(s, window=20)
    assert z > 2.0


def test_z_score_empty_nan():
    s = pd.Series([], dtype=float)
    assert math.isnan(calculate_z_score(s, window=20))


def test_absorption_div_zero_and_panic():
    assert math.isinf(calculate_absorption_ratio(2.0, 0.0))
    assert calculate_absorption_ratio(0.0, 0.0) == 0.0
    assert calculate_absorption_ratio(3_000_000.0, 2_000_000.0) == 1.5


def test_pcr_call_zero():
    assert math.isinf(get_pcr(1.0, 0.0))
    assert math.isnan(get_pcr(0.0, 0.0))


def test_find_oi_walls_max_strike():
    df = pd.DataFrame({"strike": [100, 200, 300], "oi": [10, 999, 50]})
    w = find_oi_walls(df)
    assert w["strike"] == 200.0
    assert w["oi"] == 999.0


def test_find_oi_walls_skewed_itm():
    df = pd.DataFrame({"strike": [18000, 22000, 25000], "oi": [1e9, 500, 2e5]})
    w = find_oi_walls(df)
    assert w["strike"] == 18000.0


def test_find_call_put_oi_walls_separate_sides():
    call_df = pd.DataFrame({"strike": [100.0, 110.0], "oi": [100.0, 500.0]})
    put_df = pd.DataFrame({"strike": [90.0, 100.0], "oi": [200.0, 800.0]})
    w = find_call_put_oi_walls(call_df, put_df)
    assert w["call_wall_strike"] == 110.0
    assert w["put_wall_strike"] == 100.0


def test_equity_technical_score_range():
    s = calculate_equity_technical_score(0.0, 1.5)
    assert 0.0 <= s <= 100.0


def test_intent_score_range():
    score = calculate_intent_score(1.0, 0.0, 1.0)
    assert 0.0 <= score <= 100.0

def test_calculate_ema_last_value():
    s = pd.Series([100.0, 101.0, 102.0, 103.0])
    ema = calculate_ema(s, span=3)
    assert ema > 0.0
    assert ema < 103.0


def test_calculate_atr_positive():
    df = pd.DataFrame(
        {
            "high": [101.0, 103.0, 104.0, 106.0],
            "low": [99.0, 100.0, 101.0, 103.0],
            "close": [100.0, 102.0, 103.0, 105.0],
        }
    )
    atr = calculate_atr(df, window=3)
    assert atr > 0.0


def test_calculate_adx_positive():
    df = pd.DataFrame(
        {
            "high": [101.0, 103.0, 104.5, 105.0, 106.0, 107.0, 108.0, 108.5],
            "low": [99.0, 100.5, 101.0, 102.0, 103.0, 104.0, 104.5, 105.0],
            "close": [100.0, 102.0, 103.5, 104.0, 105.0, 106.0, 107.0, 108.0],
        }
    )
    adx = calculate_adx(df, window=5)
    assert not math.isnan(adx)
    assert adx >= 0.0


def test_breakout_20d_distances_pct():
    closes = [100.0, 102.0, 101.0, 103.0, 104.0]
    df = pd.DataFrame({"close": closes})
    to_high, above_low = calculate_breakout_20d_distances_pct(df)
    assert to_high == pytest.approx(0.0)
    assert above_low == pytest.approx(4.0)


def test_calculate_atr_ratio_nan_safe_zero_divisor():
    df = pd.DataFrame(
        {
            "high": [100.0, 100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
        }
    )
    ratio = calculate_atr_ratio(df, short_window=3, long_window=4)
    assert math.isnan(ratio)


def test_calculate_cmf_and_obv_slope():
    df = pd.DataFrame(
        {
            "high": [101, 102, 103, 104, 105, 106],
            "low": [99, 100, 100, 101, 102, 103],
            "close": [100, 101, 102, 103, 104, 105],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
        }
    )
    cmf = calculate_cmf(df, window=5)
    obv_slope = calculate_obv_slope(df, window=5)
    assert not math.isnan(cmf)
    assert not math.isnan(obv_slope)
