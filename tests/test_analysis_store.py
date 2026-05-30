import math
from datetime import date
from unittest.mock import MagicMock, patch

from analysis_store import (
    _evaluate_transition_horizon_outcome,
    build_reconcile_digest_lines,
    build_comparison_payload,
    build_sector_daily_rollup,
    build_stock_reconcile_snapshot,
    build_stock_signal_transition_analytics_row,
    build_symbol_daily_feature,
    enrich_audits_with_stock_reconcile,
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
        "high_volume_down_day_proxy": True,
        "panic_absorption_proxy": True,
        "return_5d_pct": 2.1,
        "next_week_score": 58.0,
        "sell_signal": "exit",
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
    assert "high-vol-down-day" in row["flags"]
    assert "tape_extras" in row
    assert row["tape_extras"]["return_5d_pct"] == 2.1
    assert row["action_signal"] == "exit-risk"


def test_build_stock_signal_transition_analytics_row_computes_transition_and_ratios():
    history_rows = [
        {"trade_date": "2026-04-01", "action_signal": "hold", "return_1d_pct": 0.2},
        {"trade_date": "2026-04-02", "action_signal": "hold", "return_1d_pct": -0.1},
        {"trade_date": "2026-04-03", "action_signal": "buy", "return_1d_pct": 0.8},
        {"trade_date": "2026-04-04", "action_signal": "buy", "return_1d_pct": 0.7},
        {"trade_date": "2026-04-05", "action_signal": "trim", "return_1d_pct": -0.4},
        {"trade_date": "2026-04-06", "action_signal": "buy", "return_1d_pct": 0.9},
        {"trade_date": "2026-04-07", "action_signal": "buy", "return_1d_pct": 0.3},
    ]
    row = build_stock_signal_transition_analytics_row(
        sector="defence",
        symbol="HAL",
        exchange="NSE",
        run_id="defence-20260407-100000",
        as_of_trade_date=date.fromisoformat("2026-04-07"),
        historical_feature_rows=history_rows,
        trailing_window_days=30,
    )
    assert row["previous_signal"] == "trim"
    assert row["current_signal"] == "buy"
    assert row["transition_type"] == "trim_to_buy"
    assert row["transition_date"] == "2026-04-06"
    assert row["days_in_previous_signal"] == 1
    assert row["buy_signal_consistency_ratio"] > row["trim_signal_consistency_ratio"]
    assert row["whipsaw_transition_count"] >= 1
    assert 0.0 <= row["transition_stability_score"] <= 100.0


def test_build_stock_signal_transition_analytics_row_reports_matured_outcomes():
    history_rows = []
    history_rows.append({"trade_date": "2026-04-01", "action_signal": "hold", "return_1d_pct": 0.0})
    history_rows.append({"trade_date": "2026-04-02", "action_signal": "buy", "return_1d_pct": 0.1})
    for day in range(3, 29):
        history_rows.append(
            {
                "trade_date": f"2026-04-{day:02d}",
                "action_signal": "buy",
                "return_1d_pct": 0.4,
            }
        )
    row = build_stock_signal_transition_analytics_row(
        sector="defence",
        symbol="BHEL",
        exchange="NSE",
        run_id="defence-20260428-100000",
        as_of_trade_date=date.fromisoformat("2026-04-28"),
        historical_feature_rows=history_rows,
        trailing_window_days=30,
    )
    assert row["transition_type"] == "hold_to_buy"
    assert row["matured_1w_available"] is True
    assert row["matured_1m_available"] is True
    assert row["matured_1w_outcome"] == "favorable"
    assert row["matured_1m_outcome"] == "favorable"


def test_evaluate_transition_horizon_outcome_respects_signal_direction():
    assert _evaluate_transition_horizon_outcome("buy", 0.5) == "favorable"
    assert _evaluate_transition_horizon_outcome("buy", -0.6) == "unfavorable"
    assert _evaluate_transition_horizon_outcome("trim", -0.6) == "favorable"
    assert _evaluate_transition_horizon_outcome("exit-risk", 0.6) == "unfavorable"


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


def test_build_stock_reconcile_snapshot_computes_hits():
    audits = [
        {
            "symbol": "HAL",
            "sell_signal": "buy",
            "next_week_score": 65.0,
            "return_1d_pct": 1.2,
            "return_5d_pct": 2.8,
            "news_correlation": {
                "direction": "tailwind",
                "driver": "Defense orders",
                "affected_metric": "trend",
                "confidence": 0.77,
            },
        }
    ]
    historical = {
        "HAL": [
            {"tape_extras": {"next_day_score": 61.0, "next_week_score": 62.0, "sell_signal": "hold"}},
            {"tape_extras": {}},
            {"tape_extras": {}},
            {"tape_extras": {}},
            {"tape_extras": {"next_week_score": 59.0}},
        ]
    }
    snap = build_stock_reconcile_snapshot(audits, historical_rows_by_symbol=historical)
    assert snap["symbol_count"] == 1
    assert snap["coverage_next_day"] == 1
    assert snap["coverage_next_week"] == 1
    buy = snap["accuracy_by_signal_horizon"]["buy"]
    assert buy["next_day"]["hit"] == 1
    assert buy["next_week"]["hit"] == 1
    row = snap["per_symbol"]["HAL"]
    assert row["transition_quality"] == "improving"
    assert "news:" in row["news_summary"]


def test_build_reconcile_digest_lines_contains_sections():
    summary = {
        "symbol_count": 2,
        "coverage_next_day": 2,
        "coverage_next_week": 1,
        "accuracy_by_signal_horizon": {
            "buy": {"next_day": {"hit": 1, "total": 1}, "next_week": {"hit": 1, "total": 1}},
            "hold": {"next_day": {"hit": 0, "total": 1}, "next_week": {"hit": 0, "total": 0}},
        },
        "transition_quality": {"improving": 1, "stable": 1, "deteriorating": 0},
        "news_attribution_summaries": [{"symbol": "HAL", "signal": "buy", "summary": "news: tailwind"}],
    }
    lines = build_reconcile_digest_lines(summary)
    text = "\n".join(lines)
    assert "--- Stock-level reconcile ---" in text
    assert "buy: 1D 1/1 (100.0%)" in text
    assert "Transition quality:" in text
    assert "News attribution highlights:" in text


def test_enrich_audits_with_stock_reconcile_updates_audit(monkeypatch):
    audits = [
        {
            "symbol": "HAL",
            "sell_signal": "buy",
            "next_week_score": 60.0,
            "return_1d_pct": 0.8,
            "return_5d_pct": 2.0,
        }
    ]
    monkeypatch.setattr(
        "analysis_store._fetch_symbol_history_by_sector",
        lambda cfg, sector, lookback_days=45: {
            "HAL": [{"tape_extras": {"next_day_score": 58.0, "next_week_score": 52.0, "sell_signal": "hold"}}]
        },
    )
    summary = enrich_audits_with_stock_reconcile(_Cfg(), sector="defence", audits=audits)
    assert summary["symbol_count"] == 1
    assert "reconcile_next_day_hit" in audits[0]
    assert "reconcile_transition_quality" in audits[0]
