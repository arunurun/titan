from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_backtest import analyze_missed_breakouts_for_stock  # noqa: E402
from breakout_scanner import collect_filter_failures  # noqa: E402


def _synthetic_df(n: int = 80) -> dict:
    close = [40.0 + i * 0.15 for i in range(n - 2)]
    close.append(close[-1] * 1.04)
    close.append(close[-1] * 1.05)
    volume = [50000.0] * (n - 1) + [250000.0]
    high = [c * 1.02 for c in close]
    low = [c * 0.98 for c in close]
    open_ = [close[i - 1] if i else close[0] for i in range(n)]
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timestamp": list(range(n)),
    }


def test_collect_filter_failures_includes_v7_liquidity():
    eval_result = {
        "latest_price": 100.0,
        "pct_change": 5.0,
        "sma50_last": 90.0,
        "sma20_last": 95.0,
        "vol_mult": 4.0,
        "vol_cum_mult": 2.0,
        "prior_volume_spike": False,
        "rsi_val": 60.0,
        "adx_val": 30.0,
        "target_gain": 12.0,
        "evidence": {
            "liquidity_gate_pass": False,
            "liquidity_gate_fail": "pre_filter_liquidity",
        },
    }
    fails = collect_filter_failures(eval_result, "SMALL_CAP_100")
    assert "pre_filter_liquidity" in fails


def test_analyze_missed_uses_last_pass_idx_and_turnover(monkeypatch):
    n = 80
    df = _synthetic_df(n)
    calls: list[dict] = []

    def _spy_evaluate(df_in, idx, tier_key, **kwargs):
        calls.append({"idx": idx, **kwargs})
        return {
            "passed": False,
            "fail_reason": "RSI",
            "signal_tier": None,
            "pct_change": 1.0,
            "vol_mult": 1.0,
            "rsi_val": 80.0,
            "adx_val": 25.0,
            "latest_price": 50.0,
            "target_gain": 10.0,
            "sma50_last": 45.0,
            "sma20_last": 48.0,
            "vol_cum_mult": 1.0,
            "prior_volume_spike": False,
        }

    monkeypatch.setattr("breakout_backtest.evaluate_bars_as_of", _spy_evaluate)
    monkeypatch.setattr(
        "breakout_backtest._forward_max_return_pct",
        lambda *_a, **_k: 12.0,
    )

    stock_data = {
        "df": df,
        "dates": [f"2026-01-{i:02d}" for i in range(1, n + 1)],
        "tier_key": "SMALL_CAP_100",
        "tier_label": "Small-Cap",
        "liquidity_turnover_lacs_avg": 42.5,
    }
    report = analyze_missed_breakouts_for_stock("TEST", stock_data, min_return_pct=8.0)
    assert report["missed_count"] >= 0
    assert calls, "expected evaluate_bars_as_of to be called"
    assert calls[0].get("bhav_turnover_lacs") == 42.5
    assert "last_pass_idx" in calls[0]


def test_missed_analysis_separates_watch_bucket(monkeypatch):
    n = 80
    df = _synthetic_df(n)

    def _eval_watch(df_in, idx, tier_key, **kwargs):
        return {
            "passed": False,
            "fail_reason": None,
            "signal_tier": "WATCH",
            "v7_watch_reason": "v7_low_volume_persistence",
            "pct_change": 5.0,
            "vol_mult": 4.0,
            "rsi_val": 60.0,
            "adx_val": 28.0,
            "latest_price": 55.0,
            "target_gain": 10.0,
            "sma50_last": 50.0,
            "sma20_last": 52.0,
            "vol_cum_mult": 2.0,
            "prior_volume_spike": False,
            "evidence": {"liquidity_gate_pass": True},
        }

    monkeypatch.setattr("breakout_backtest.evaluate_bars_as_of", _eval_watch)
    monkeypatch.setattr(
        "breakout_backtest._forward_max_return_pct",
        lambda *_a, **_k: 15.0,
    )

    stock_data = {
        "df": df,
        "dates": [f"2026-02-{i:02d}" for i in range(1, n + 1)],
        "tier_key": "SMALL_CAP_100",
        "tier_label": "Small-Cap",
    }
    report = analyze_missed_breakouts_for_stock("WATCHME", stock_data, min_return_pct=8.0)
    assert report["watch_missed_count"] > 0
    assert report["missed_count"] == 0
    assert report["watch_fail_reason_counts"].get("v7_low_volume_persistence", 0) > 0
