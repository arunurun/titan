"""Unit tests for precious_metals_algo DXY × GSR-band allocator."""

import math

import pandas as pd
import pytest

from precious_metals_algo import (
    PreciousMetalsAlgo,
    classify_dxy,
    classify_gsr_band,
    classify_sge_state,
    generate_synthetic_pm_macro_series,
)


def _make_series(base: float, n: int = 260, drift: float = 0.0) -> pd.Series:
    values = [base + drift * i for i in range(n)]
    return pd.Series(values)


def test_init_rejects_invalid_params():
    with pytest.raises(ValueError, match="z_window"):
        PreciousMetalsAlgo(z_window=1)
    with pytest.raises(ValueError, match="z_threshold"):
        PreciousMetalsAlgo(z_threshold=0.0)


def test_generate_features_requires_core_series():
    algo = PreciousMetalsAlgo(z_window=20)
    gold = _make_series(2000.0, n=30)
    silver = _make_series(25.0, n=30)
    with pytest.raises(ValueError, match="DXY"):
        algo.generate_features({"GOLD": gold, "SILVER": silver})


def test_classify_dxy_and_gsr_band():
    assert classify_dxy(-1.2) == "WEAK"
    assert classify_dxy(1.2) == "STRONG"
    assert classify_dxy(0.2) == "NEUTRAL"
    assert classify_gsr_band(45.0) == "below"
    assert classify_gsr_band(55.0) == "in"
    assert classify_gsr_band(72.0) == "above"


def test_weak_dxy_high_gsr_allocation():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0)
    dxy = pd.Series([110.0 - i * 0.5 for i in range(30)])
    gold = pd.Series([2000.0 + i * 20.0 for i in range(30)])
    silver = pd.Series([25.0] * 30)
    features = algo.generate_features({"GOLD": gold, "SILVER": silver, "DXY": dxy})
    assert features["dxy_z"] <= -1.0
    assert features["gsr_last"] > 60.0

    out = algo.execute_allocation_logic(features)
    assert out["dxy_state"] == "WEAK"
    assert out["gsr_band"] == "above"
    assert out["base_gold_pct"] == pytest.approx(17.0, abs=0.1)
    assert out["base_silver_pct"] == pytest.approx(68.0, abs=0.1)
    assert out["base_cash_pct"] == pytest.approx(15.0, abs=0.1)
    assert out["gold_pct"] + out["silver_pct"] + out["cash_pct"] == pytest.approx(100.0, abs=0.1)


def test_strong_dxy_neutral_gsr_allocation():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0)
    dxy = pd.Series([90.0 + i * 0.5 for i in range(30)])
    gold = pd.Series([2000.0] * 30)
    silver = pd.Series([40.0] * 30)
    features = algo.generate_features({"GOLD": gold, "SILVER": silver, "DXY": dxy})
    assert features["dxy_z"] >= 1.0

    out = algo.execute_allocation_logic(features)
    assert out["dxy_state"] == "STRONG"
    assert out["gsr_band"] == "in"
    assert out["base_gold_pct"] == pytest.approx(11.0, abs=0.1)
    assert out["base_silver_pct"] == pytest.approx(9.0, abs=0.1)
    assert out["base_cash_pct"] == pytest.approx(80.0, abs=0.1)


def test_sge_tightness_boosts_metals_exposure():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0, sge_z_threshold=1.0)
    features = {
        "dxy_z": -1.5,
        "gsr_z": 0.0,
        "gsr_last": 72.0,
        "sge_premium_z": 1.5,
        "sge_withdrawal_z": float("nan"),
    }
    out = algo.execute_allocation_logic(features)
    assert out["dxy_state"] == "WEAK"
    assert out["sge_state"] == "TIGHT"
    assert out["gold_pct"] + out["silver_pct"] > out["base_gold_pct"] + out["base_silver_pct"]
    assert "sge_tightness" in out["sge_adjustments"]


def test_sge_tightness_floors_metals_in_strong_dxy():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0, sge_z_threshold=1.0)
    features = {
        "dxy_z": 1.5,
        "gsr_z": 0.0,
        "gsr_last": 55.0,
        "sge_premium_z": 1.2,
        "sge_withdrawal_z": float("nan"),
    }
    out = algo.execute_allocation_logic(features)
    assert out["dxy_state"] == "STRONG"
    assert out["sge_state"] == "TIGHT"
    assert out["gold_pct"] >= 20.0
    assert out["silver_pct"] >= 10.0
    assert "sge_bear_floor" in out["sge_adjustments"]


def test_sge_weak_demand_reduces_metals():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0, sge_z_threshold=1.0)
    features = {
        "dxy_z": 0.0,
        "gsr_z": 0.0,
        "gsr_last": 55.0,
        "sge_premium_z": -1.5,
        "sge_withdrawal_z": float("nan"),
    }
    out = algo.execute_allocation_logic(features)
    assert out["dxy_state"] == "NEUTRAL"
    assert out["sge_state"] == "WEAK"
    assert out["gold_pct"] < out["base_gold_pct"]
    assert out["cash_pct"] > out["base_cash_pct"]
    assert "sge_weak_demand" in out["sge_adjustments"]


def test_synthetic_fixture_end_to_end():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0, sge_z_threshold=1.0)
    data = generate_synthetic_pm_macro_series(n=35)
    features = algo.generate_features(data)
    out = algo.execute_allocation_logic(features)
    assert out["conviction"] in ("HIGH", "MODERATE", "MIXED", "LOW")
    assert out["read_line"]
    assert len(out["waterfall_steps"]) == 3


def test_sge_premium_from_sge_gold_series():
    algo = PreciousMetalsAlgo(z_window=20)
    gold = pd.Series([2000.0] * 30)
    silver = pd.Series([25.0] * 30)
    dxy = pd.Series([100.0] * 30)
    sge_gold = pd.Series([2040.0] * 30)
    features = algo.generate_features(
        {"GOLD": gold, "SILVER": silver, "DXY": dxy, "SGE_GOLD": sge_gold}
    )
    assert features["sge_premium_pct"] == pytest.approx(2.0, abs=0.01)


def test_sge_premium_pct_column_direct():
    algo = PreciousMetalsAlgo(z_window=20)
    gold = pd.Series([2000.0] * 30)
    silver = pd.Series([25.0] * 30)
    dxy = pd.Series([100.0] * 30)
    premium = pd.Series([1.5] * 30)
    features = algo.generate_features(
        {
            "GOLD": gold,
            "SILVER": silver,
            "DXY": dxy,
            "SGE_PREMIUM_PCT": premium,
        }
    )
    assert features["sge_premium_pct"] == pytest.approx(1.5)


def test_waterfall_audit_trail_present():
    algo = PreciousMetalsAlgo(z_window=20)
    out = algo.execute_allocation_logic(
        {"dxy_z": -1.2, "gsr_z": 0.0, "gsr_last": 72.0, "sge_premium_z": float("nan")}
    )
    assert out["waterfall"][0].startswith("dxy:")
    assert any(step.startswith("final=") for step in out["waterfall"])


def test_weights_always_sum_to_100():
    algo = PreciousMetalsAlgo(z_window=20)
    cases = [
        {"dxy_z": 2.0, "gsr_z": 2.0, "gsr_last": 72.0, "sge_premium_z": 2.0, "sge_withdrawal_z": 2.0},
        {"dxy_z": -2.0, "gsr_z": -2.0, "gsr_last": 45.0, "sge_premium_z": -2.0, "sge_withdrawal_z": float("nan")},
        {"dxy_z": float("nan"), "gsr_z": float("nan"), "gsr_last": float("nan"), "sge_premium_z": float("nan"), "sge_withdrawal_z": float("nan")},
    ]
    for features in cases:
        out = algo.execute_allocation_logic(features)
        total = out["gold_pct"] + out["silver_pct"] + out["cash_pct"]
        assert total == pytest.approx(100.0, abs=0.05)
