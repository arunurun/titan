from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_evidence import (  # noqa: E402
    MICRO_CAP_MIN_MEDIAN_TURNOVER_INR,
    SMALL_CAP_MIN_MEDIAN_TURNOVER_INR,
    base_quality_score,
    breakout_stage,
    composite_rank_score,
    compute_evidence_metrics,
    delivery_anomaly_required_pct,
    liquidity_gate_fail_reason,
    liquidity_gate_pass,
    liquidity_quality_score,
    micro_cap_participation_pass,
    micro_cap_stricter_rules,
    persistence_pass_min,
    relative_strength_rank_subscore,
    relative_strength_vs_benchmark,
    return_5d_pct,
    volume_persistence_score,
)


def test_liquidity_gate_pass_tiers():
    assert liquidity_gate_pass("SMALL_CAP_100", SMALL_CAP_MIN_MEDIAN_TURNOVER_INR) is True
    assert liquidity_gate_pass("SMALL_CAP_100", SMALL_CAP_MIN_MEDIAN_TURNOVER_INR - 1) is False
    assert liquidity_gate_pass("MICRO_CAP_250", None) is False
    assert liquidity_gate_pass("MICRO_CAP_250", None, session_notional_inr=35_000_000) is True


def test_liquidity_gate_missing_data_fail_reason():
    assert liquidity_gate_fail_reason("SMALL_CAP_100", None) == "missing_liquidity_data"
    assert liquidity_gate_fail_reason("SMALL_CAP_100", None, session_notional_inr=500_000) == "pre_filter_liquidity"


def test_liquidity_quality_score_weighted():
    score = liquidity_quality_score(50_000_000, 60.0, 80.0, 40.0)
    assert 0 <= score <= 100


def test_volume_persistence_score_buckets():
    vols = [100.0] * 7 + [200.0, 200.0, 200.0]
    assert volume_persistence_score(vols, 100.0, lookback=10) == 4


def test_breakout_stage_classification():
    assert breakout_stage(True, 35, 1.5, 5) == 1
    assert breakout_stage(False, 10, 5.0, 25) == 3
    assert breakout_stage(False, 10, 2.5, 10) == 2


def test_base_quality_score_range():
    score = base_quality_score(25, 50.0, 70.0, 90.0)
    assert 0 <= score <= 100


def test_composite_rank_score_stub_sector():
    metrics = {
        "vol_mult": 4.0,
        "pct_change": 6.0,
        "base_score": 70.0,
        "persistence_score": 2,
        "rsi_val": 60.0,
        "pass_paths": [],
        "breakout_stage": 1,
    }
    rank = composite_rank_score(metrics)
    assert 0 <= rank <= 100
    assert composite_rank_score(metrics, sector_lead=80.0) >= rank


def test_micro_cap_stricter_rules():
    rules = micro_cap_stricter_rules("MICRO_CAP_250")
    assert rules["vpr_min"] == 2.0
    assert rules["delivery_pct_min"] == 40.0
    assert rules["median_turnover_inr_min"] == MICRO_CAP_MIN_MEDIAN_TURNOVER_INR


def test_persistence_pass_min():
    assert persistence_pass_min("MICRO_CAP_250") == 2
    assert persistence_pass_min("SMALL_CAP_100") == 1


def test_micro_cap_participation_pass_delivery_required():
    assert micro_cap_participation_pass("MICRO_CAP_250", vpr=3.0, cmf=0.1, delivery_pct=50.0) is True
    assert micro_cap_participation_pass("MICRO_CAP_250", vpr=3.0, cmf=0.1, delivery_pct=30.0) is False
    assert micro_cap_participation_pass("SMALL_CAP_100", vpr=2.0, cmf=0.1) is True


def test_micro_cap_participation_delivery_anomaly_vs_avg():
    assert micro_cap_participation_pass(
        "MICRO_CAP_250", vpr=3.0, cmf=0.1, delivery_pct=50.0, avg_delivery_pct=60.0,
    ) is True
    assert micro_cap_participation_pass(
        "MICRO_CAP_250", vpr=3.0, cmf=0.1, delivery_pct=47.0, avg_delivery_pct=60.0,
    ) is False


def test_micro_cap_participation_bypasses_missing_day_t_delivery():
    assert micro_cap_participation_pass(
        "MICRO_CAP_250", vpr=3.0, cmf=0.1, delivery_pct=None, avg_delivery_pct=55.0,
    ) is True


def test_delivery_anomaly_required_pct_uses_max_of_static_and_dynamic():
    assert delivery_anomaly_required_pct(50.0, 60.0) == 48.0
    assert delivery_anomaly_required_pct(50.0, 30.0) == 40.0


def test_relative_strength_vs_benchmark_and_rank_bonus():
    rel = relative_strength_vs_benchmark(8.0, 1.0)
    assert rel == 7.0
    base = relative_strength_rank_subscore(rel, benchmark_5d_return=1.0)
    bonus = relative_strength_rank_subscore(rel, benchmark_5d_return=-0.5)
    assert bonus > base


def test_return_5d_pct():
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 105.0]
    assert return_5d_pct(closes, 5) == 5.0


def test_composite_rank_prefers_relative_strength_over_rsi():
    base_metrics = {
        "vol_mult": 4.0,
        "pct_change": 6.0,
        "base_score": 70.0,
        "persistence_score": 2,
        "rsi_val": 55.0,
        "pass_paths": [],
        "breakout_stage": 1,
        "benchmark_5d_return": -1.0,
    }
    weak_rs = dict(base_metrics, rel_return_5d_vs_benchmark=0.0)
    strong_rs = dict(base_metrics, rel_return_5d_vs_benchmark=8.0)
    assert composite_rank_score(strong_rs) > composite_rank_score(weak_rs)


def test_compute_evidence_metrics_includes_flow_and_delivery():
    n = 60
    close = [50.0 + i * 0.1 for i in range(n)]
    volume = [10000.0] * (n - 1) + [25000.0]
    df = {
        "open": close[:],
        "high": [c + 0.5 for c in close],
        "low": [c - 0.5 for c in close],
        "close": close,
        "volume": volume,
    }
    vol_20 = [10000.0] * n
    out = compute_evidence_metrics(
        df,
        n - 1,
        "MICRO_CAP_250",
        vol_20,
        delivery_pct=55.0,
        free_float_pct=35.0,
    )
    assert out["delivery_pct"] == 55.0
    assert out["free_float_pct"] == 35.0
    assert out.get("vpr") is not None
    assert out.get("cmf") is not None
    assert out["micro_participation_pass"] is not None
    assert out["liquidity_quality"] != 50.0


def test_compute_evidence_metrics_pending_delivery_flag_for_micro():
    n = 60
    close = [50.0 + i * 0.1 for i in range(n)]
    volume = [10000.0] * n
    df = {
        "open": close[:],
        "high": [c + 0.5 for c in close],
        "low": [c - 0.5 for c in close],
        "close": close,
        "volume": volume,
    }
    vol_20 = [10000.0] * n
    out = compute_evidence_metrics(
        df,
        n - 1,
        "MICRO_CAP_250",
        vol_20,
        delivery_pct=None,
        avg_delivery_pct=52.0,
        free_float_pct=35.0,
    )
    assert out["participation_risk_flag"] == "PENDING_DELIVERY_DATA"
    assert out["avg_delivery_pct"] == 52.0


def test_compute_evidence_inputs_excludes_day_t_from_base_metrics():
    from breakout_evidence import compute_evidence_inputs

    n = 60
    close = [50.0] * (n - 1) + [80.0]
    highs = [51.0] * (n - 1) + [80.0]
    lows = [49.0] * n
    df = {
        "open": close[:],
        "high": highs,
        "low": lows,
        "close": close,
        "volume": [10000.0] * n,
    }
    inputs = compute_evidence_inputs(df, n - 1)
    assert inputs["pivot_proximity"] is not None
    assert inputs["pivot_proximity"] > 90.0
