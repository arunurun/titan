import math

import pandas as pd
import pytest

from titan_engine import (
    calculate_absorption_ratio,
    calculate_intent_score,
    calculate_z_score,
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


def test_intent_score_range():
    score = calculate_intent_score(1.0, 0.0, 1.0)
    assert 0.0 <= score <= 100.0
