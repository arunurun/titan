from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_evidence import setup_rank_score  # noqa: E402
from breakout_setup import (  # noqa: E402
    SETUP_BASE_SCORE_MIN,
    SETUP_PCT_CHANGE_MAX,
    SETUP_PCT_CHANGE_MIN,
    SIGNAL_TIER_PRE_BREAKOUT,
    evaluate_setup_as_of,
)


def _synthetic_df(*, n: int = 80, close: float = 100.0, volume: float = 1_000_000.0) -> dict:
    return {
        "open": [close] * n,
        "high": [close * 1.01] * n,
        "low": [close * 0.99] * n,
        "close": [close] * n,
        "volume": [volume] * n,
        "timestamp": list(range(n)),
    }


def test_setup_rank_score_penalizes_participation():
    base = setup_rank_score({
        "base_score": 70,
        "persistence_score": 2,
        "sector_lead": 60,
        "pivot_proximity": 90,
        "liquidity_quality": 65,
        "rsi_val": 55,
        "participation_penalty": 0,
    })
    penalized = setup_rank_score({
        "base_score": 70,
        "persistence_score": 2,
        "sector_lead": 60,
        "pivot_proximity": 90,
        "liquidity_quality": 65,
        "rsi_val": 55,
        "participation_penalty": 15,
    })
    assert penalized < base


def test_evaluate_setup_rejects_high_pct_change():
    df = _synthetic_df()
    result = evaluate_setup_as_of(
        df,
        len(df["close"]) - 1,
        "SMALL_CAP_100",
        min_price=15.0,
        vol_mult_threshold=3.5,
        pct_change=5.0,
        vol_mult=2.0,
        rsi_val=55.0,
        adx_val=22.0,
        sma20_last=99.0,
        sma50_last=98.0,
    )
    assert result is None


def test_evaluate_setup_pct_band_constants():
    assert SETUP_PCT_CHANGE_MIN < 0
    assert SETUP_PCT_CHANGE_MAX < 3.0
    assert SETUP_BASE_SCORE_MIN >= 50


def test_cap_setup_candidates_per_tier():
    from breakout_scanner import _cap_setup_candidates_per_tier
    from breakout_setup import SIGNAL_TIER_PRE_BREAKOUT

    rows = [
        {"Ticker": f"S{i}", "Tier": "Small-Cap (Nifty Smallcap 100)", "Signal Tier": SIGNAL_TIER_PRE_BREAKOUT, "Setup Rank": float(i)}
        for i in range(15)
    ]
    capped = _cap_setup_candidates_per_tier(rows, cap=10)
    setup_only = [r for r in capped if r.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT]
    assert len(setup_only) == 10
    assert max(r["Setup Rank"] for r in setup_only) == 14.0


def test_setup_to_breakout_rate_empty():
    from breakout_backtest import setup_to_breakout_rate

    summary = setup_to_breakout_rate([])
    assert summary["total_setup_signals"] == 0
    assert summary["precision_t5"] is None


def test_setup_to_breakout_rate_precision():
    from breakout_backtest import setup_to_breakout_rate

    setups = [
        {"horizons": {"t5": {"breakout_hit": True}, "t10": {"breakout_hit": True}, "t15": {"breakout_hit": True, "forward_breakout_session": 3}}},
        {"horizons": {"t5": {"breakout_hit": False}, "t10": {"breakout_hit": False}, "t15": {"breakout_hit": False}}},
    ]
    summary = setup_to_breakout_rate(setups)
    assert summary["total_setup_signals"] == 2
    assert summary["hits_t5"] == 1
    assert summary["precision_t5"] == 50.0
    assert summary["median_lead_sessions_t15"] == 3
