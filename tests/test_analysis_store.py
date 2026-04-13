import math

from analysis_store import build_sector_daily_rollup, build_symbol_daily_feature


def test_build_symbol_daily_feature_basic():
    audit = {
        "symbol": "HAL",
        "exchange": "NSE",
        "intent_score": 61.2,
        "effective_intent_score": 55.0,
        "z_score": 2.3,
        "absorption_ratio": 1.4,
        "return_1d_pct": 1.2,
        "ema_200_distance_pct": 4.5,
        "atr_14_pct": 3.2,
        "rows": 38,
        "trap_exit_proxy": False,
        "panic_absorption_proxy": True,
    }
    row = build_symbol_daily_feature(
        audit,
        trade_date="2026-04-12",
        sector="defence",
        run_id="defence-20260412-100000",
        run_ts_iso="2026-04-12T10:00:00+05:30",
    )
    assert row["symbol"] == "HAL"
    assert row["rows_count"] == 38
    assert "panic-absorption" in row["flags"]


def test_build_sector_daily_rollup_shapes_metrics():
    audits = [
        {
            "intent_score": 70.0,
            "effective_intent_score": 50.0,
            "z_score": 2.5,
            "absorption_ratio": 1.2,
            "ema_200_distance_pct": 2.0,
            "trap_exit_proxy": False,
            "panic_absorption_proxy": False,
            "macro_guardrail_applied": True,
        },
        {
            "intent_score": 40.0,
            "effective_intent_score": 34.0,
            "z_score": 0.8,
            "absorption_ratio": 0.6,
            "ema_200_distance_pct": -1.0,
            "trap_exit_proxy": True,
            "panic_absorption_proxy": False,
            "macro_guardrail_applied": False,
        },
    ]
    r = build_sector_daily_rollup(
        audits,
        trade_date="2026-04-12",
        sector="defence",
        run_id="defence-20260412-100000",
        run_ts_iso="2026-04-12T10:00:00+05:30",
    )
    assert r["symbol_count"] == 2
    assert math.isclose(r["avg_intent_score"], 55.0)
    assert r["trap_count"] == 1
    assert r["macro_guardrail_count"] == 1
