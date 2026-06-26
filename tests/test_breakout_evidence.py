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
    liquidity_gate_pass,
    liquidity_quality_score,
    micro_cap_stricter_rules,
    persistence_pass_min,
    volume_persistence_score,
)


def test_liquidity_gate_pass_tiers():
    assert liquidity_gate_pass("SMALL_CAP_100", SMALL_CAP_MIN_MEDIAN_TURNOVER_INR) is True
    assert liquidity_gate_pass("SMALL_CAP_100", SMALL_CAP_MIN_MEDIAN_TURNOVER_INR - 1) is False
    assert liquidity_gate_pass("MICRO_CAP_250", None) is True


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
