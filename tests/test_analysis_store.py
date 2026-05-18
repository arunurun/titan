import math
from unittest.mock import MagicMock, patch

from analysis_store import (
    build_comparison_payload,
    build_sector_daily_rollup,
    build_symbol_daily_feature,
    quality_checks_for_run,
    update_sector_period_rollups,
)


class _Cfg:
    supabase_url = "https://x.supabase.co"
    supabase_key = "service"


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
    assert "panic-vol-down-day" in row["flags"]


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


def test_quality_checks_flags_short_history():
    audits = [{"rows": 10}, {"rows": 38}]
    checks = quality_checks_for_run(audits)
    assert "short_history_for_some_symbols" in checks


def test_update_sector_period_rollups_upserts_rows(monkeypatch):
    monkeypatch.setenv("TITAN_ENABLE_ANALYSIS_STORE", "1")
    mock_client = MagicMock()
    mock_rows = [
        {
            "trade_date": "2026-04-10",
            "avg_intent_score": 50.0,
            "avg_effective_intent_score": 48.0,
            "breadth_above_ema200_pct": 55.0,
            "pct_z_gt_2": 10.0,
            "pct_absorption_gt_1": 45.0,
            "trap_count": 1,
            "panic_absorption_count": 0,
        },
        {
            "trade_date": "2026-04-11",
            "avg_intent_score": 52.0,
            "avg_effective_intent_score": 50.0,
            "breadth_above_ema200_pct": 57.0,
            "pct_z_gt_2": 12.0,
            "pct_absorption_gt_1": 47.0,
            "trap_count": 0,
            "panic_absorption_count": 0,
        },
    ]
    mock_client.table.return_value.select.return_value.eq.return_value.gte.return_value.lte.return_value.order.return_value.execute.return_value = MagicMock(
        data=mock_rows
    )
    with patch("analysis_store.create_client", return_value=mock_client):
        out = update_sector_period_rollups(_Cfg(), sector="defence", as_of_date="2026-04-11")
    assert out["updated"] is True
    assert mock_client.table.return_value.upsert.call_count >= 1


def test_build_comparison_payload_basic(monkeypatch):
    monkeypatch.setenv("TITAN_ENABLE_ANALYSIS_STORE", "1")
    mock_client = MagicMock()
    daily_tbl = MagicMock()
    daily_tbl.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"symbol_count": 10, "avg_effective_intent_score": 52.0}]
    )
    period_tbl = MagicMock()
    period_tbl.select.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[
            {"period_type": "7d", "avg_effective_intent_score": 50.0},
            {"period_type": "30d", "avg_effective_intent_score": 48.0},
        ]
    )
    leaders_tbl = MagicMock()
    leaders_tbl.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"symbol": "A", "effective_intent_score": 70.0}]
    )
    lag_tbl = MagicMock()
    lag_tbl.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"symbol": "B", "effective_intent_score": 40.0}]
    )
    mock_client.table.side_effect = [daily_tbl, period_tbl, leaders_tbl, lag_tbl]
    with patch("analysis_store.create_client", return_value=mock_client):
        payload = build_comparison_payload(_Cfg(), sector="defence", as_of_date="2026-04-11")
    assert payload["enabled"] is True
    assert payload["delta"]["avg_effective_intent_vs_7d"] == 2.0
