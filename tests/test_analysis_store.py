import math
from datetime import date
from unittest.mock import MagicMock, patch

from analysis_store import (
    _evaluate_transition_horizon_outcome,
    _safe_tape_extras,
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


def test_build_symbol_daily_feature_persists_ohlc_session_fields():
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
        "ohlc_bar_as_of_date": "2026-06-05",
        "ohlc_bar_incomplete": True,
        "session_move_vs_prev_close_pct": -1.5,
        "price_snapshot_ts": "06-Jun-2026 14:32:00",
    }
    row = build_symbol_daily_feature(
        audit,
        trade_date="2026-06-06",
        sector="defence",
        run_id="defence-20260606-100000",
        run_ts_iso="2026-06-06T10:00:00+05:30",
    )
    assert row["trade_date"] == "2026-06-06"
    assert row["tape_extras"]["ohlc_bar_as_of_date"] == "2026-06-05"
    assert row["tape_extras"]["ohlc_bar_incomplete"] is True
    assert row["tape_extras"]["session_move_vs_prev_close_pct"] == -1.5
    assert row["tape_extras"]["price_snapshot_ts"] == "06-Jun-2026 14:32:00"


def test_build_symbol_daily_feature_includes_news_columns():
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
        "news_correlation": {"direction": "tailwind", "driver": "Orders"},
        "news_sentiment_aggregate": "positive",
        "news_sentiment_score": 0.42,
        "news_sentiment_trend": "strengthening",
        "news_count": 4,
    }
    row = build_symbol_daily_feature(
        audit,
        trade_date="2026-04-12",
        sector="defence",
        run_id="defence-20260412-100000",
        run_ts_iso="2026-04-12T10:00:00+05:30",
    )
    assert row["news_sentiment_aggregate"] == "positive"
    assert row["news_sentiment_score"] == 0.42
    assert row["news_sentiment_trend"] == "strengthening"
    assert row["news_count"] == 4
    assert row["tape_extras"]["news_correlation"]["direction"] == "tailwind"


def test_build_symbol_daily_feature_denormalizes_prediction_scores():
    audit = {
        "symbol": "HAL",
        "exchange": "NSE",
        "intent_score": 61.2,
        "absorption_ratio": 1.4,
        "volume_participation_ratio": 1.304,
        "next_day_score": 49.77,
        "next_week_score": 52.0,
        "rows": 38,
    }
    row = build_symbol_daily_feature(
        audit,
        trade_date="2026-04-12",
        sector="defence",
        run_id="defence-20260412-100000",
        run_ts_iso="2026-04-12T10:00:00+05:30",
    )
    assert row["volume_participation_ratio"] == 1.304
    assert row["next_day_score"] == 49.77
    assert row["next_week_score"] == 52.0
    assert row["tape_extras"]["next_day_score"] == 49.77


def test_safe_tape_extras_merges_top_level_scores():
    merged = _safe_tape_extras(
        {"tape_extras": {"sell_signal": "hold"}, "next_day_score": 61.0, "next_week_score": 58.0}
    )
    assert merged["next_day_score"] == 61.0
    assert merged["next_week_score"] == 58.0
    assert merged["sell_signal"] == "hold"


def test_build_stock_reconcile_snapshot_uses_top_level_prediction_columns():
    table_inputs = {
        "sector": "defence",
        "as_of_trade_date": "2026-04-12",
        "symbol_daily_features": [
            {
                "trade_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "buy",
                "return_1d_pct": 1.4,
            },
            {
                "trade_date": "2026-04-11",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "hold",
                "next_day_score": 60.0,
            },
        ],
        "stock_signal_transition_analytics": [],
        "sector_daily_winners": [],
        "sector_daily_rollup": [],
        "global_news_snapshots": [],
        "llm_digest_memory": [],
    }
    snap = build_stock_reconcile_snapshot([], table_inputs=table_inputs, as_of_trade_date="2026-04-12")
    assert snap["coverage_next_day"] == 1
    assert snap["per_symbol"]["HAL"]["hit_next_day"] is True


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
        "as_of_trade_date": "2026-04-12",
        "symbol_count": 2,
        "coverage_next_day": 2,
        "coverage_next_week": 1,
        "accuracy_by_signal_horizon": {
            "buy": {"next_day": {"hit": 1, "total": 1}, "next_week": {"hit": 1, "total": 1}},
            "hold": {"next_day": {"hit": 0, "total": 1}, "next_week": {"hit": 0, "total": 0}},
        },
        "transition_outcome_1w": {"favorable": 1, "neutral": 0, "unfavorable": 0},
        "transition_outcome_1m": {"favorable": 0, "neutral": 1, "unfavorable": 0},
        "whipsaw_transition_total": 1,
        "whipsaw_symbol_rate_pct": 50.0,
        "avg_transition_stability_score": 73.4,
        "news_attribution_summaries": [{"symbol": "HAL", "signal": "buy", "summary": "news: tailwind"}],
    }
    summary["per_symbol"] = {
        "HAL": {
            "symbol": "HAL",
            "sector": "defence",
            "signal_transition": "hold->buy",
            "transition_beneficial": True,
            "transition_outcome_label": "beneficial",
            "is_success": True,
            "is_failure": False,
            "news_summary": "news: tailwind",
        },
        "BEL": {
            "symbol": "BEL",
            "sector": "defence",
            "signal_transition": "buy->hold",
            "transition_beneficial": False,
            "transition_outcome_label": "not-beneficial",
            "is_success": False,
            "is_failure": True,
            "failure_reason": "news_shock",
            "news_direction": "down",
            "news_summary": "news: headwind",
        },
    }
    lines = build_reconcile_digest_lines(summary)
    text = "\n".join(lines)
    assert "--- EOD Reconcile (Decision-first) ---" in text
    assert "A) Coverage and confidence" in text
    assert "B) Success summary" in text
    assert "C) Failure summary" in text
    assert "D) Signal transition matrix" in text
    assert "E) Key transition examples" in text
    assert "F) Actionable model-improvement flags" in text


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


def test_build_stock_reconcile_snapshot_prefers_table_inputs():
    table_inputs = {
        "sector": "defence",
        "as_of_trade_date": "2026-04-12",
        "symbol_daily_features": [
            {
                "trade_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "buy",
                "return_1d_pct": 1.4,
                "tape_extras": {
                    "return_5d_pct": 2.0,
                    "news_correlation": {"direction": "tailwind", "driver": "Orders"},
                },
            },
            {
                "trade_date": "2026-04-11",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "hold",
                "tape_extras": {"next_day_score": 60.0},
            },
            {
                "trade_date": "2026-04-06",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "hold",
                "tape_extras": {"next_week_score": 58.0},
            },
        ],
        "stock_signal_transition_analytics": [
            {
                "trade_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "previous_signal": "hold",
                "current_signal": "buy",
                "transition_type": "hold_to_buy",
                "transition_stability_score": 78.0,
                "is_whipsaw_transition": False,
                "whipsaw_transition_count": 0,
                "matured_1w_outcome": "favorable",
                "matured_1m_outcome": "favorable",
            }
        ],
        "sector_daily_winners": [
            {
                "as_of_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "winner_rank": 1,
            }
        ],
        "sector_daily_rollup": [
            {
                "trade_date": "2026-04-12",
                "symbol_count": 12,
                "avg_effective_intent_score": 55.0,
                "breadth_above_ema200_pct": 61.0,
            }
        ],
        "global_news_snapshots": [
            {
                "refreshed_at": "2026-04-12T10:00:00+00:00",
                "fetch_status": "ok",
                "sector_scores": {"defence": 0.42},
            }
        ],
        "llm_digest_memory": [{"output_text": "optional context"}],
    }
    snap = build_stock_reconcile_snapshot(
        [],
        table_inputs=table_inputs,
        as_of_trade_date="2026-04-12",
    )
    assert snap["symbol_count"] == 1
    assert snap["success_count"] == 1
    assert snap["failure_count"] == 0
    assert snap["transition_quality"]["favorable"] == 1
    assert snap["shortlist_efficacy"]["count"] == 1
    assert snap["shortlist_efficacy"]["hit_1d"] == 1
    assert snap["news_attribution_efficacy"]["evaluated_symbols"] == 1
    assert snap["news_attribution_efficacy"]["aligned_symbols"] == 1
    assert snap["per_symbol"]["HAL"]["transition_beneficial"] is True


def test_build_reconcile_digest_lines_includes_efficacy_sections():
    summary = {
        "as_of_trade_date": "2026-04-12",
        "symbol_count": 3,
        "coverage_next_day": 2,
        "coverage_next_week": 2,
        "accuracy_by_signal_horizon": {
            "buy": {"next_day": {"hit": 1, "total": 1}, "next_week": {"hit": 1, "total": 1}},
            "hold": {"next_day": {"hit": 0, "total": 1}, "next_week": {"hit": 0, "total": 1}},
        },
        "scope": "all-stocks",
        "transition_quality": {"favorable": 1, "neutral": 1},
        "transition_outcome_1w": {"favorable": 1},
        "transition_outcome_1m": {"neutral": 1},
        "whipsaw_transition_total": 1,
        "whipsaw_symbol_rate_pct": 50.0,
        "avg_transition_stability_score": 70.0,
        "shortlist_efficacy": {"count": 2, "hit_1d": 1, "hit_rate_1d_pct": 50.0, "coverage_5d": 2, "hit_5d": 1, "hit_rate_5d_pct": 50.0},
        "news_attribution_efficacy": {
            "evaluated_symbols": 2,
            "aligned_symbols": 1,
            "alignment_rate_pct": 50.0,
            "snapshot_status": "ok",
            "snapshot_refreshed_at": "2026-04-12T10:00:00+00:00",
        },
        "per_symbol": {
            "HAL": {
                "symbol": "HAL",
                "sector": "defence",
                "signal": "buy",
                "hit_next_day": True,
                "hit_next_week": True,
                "signal_transition": "hold->buy",
                "transition_quality": "favorable",
                "transition_beneficial": True,
                "transition_outcome_label": "beneficial",
                "is_success": True,
                "is_failure": False,
                "news_summary": "news: tailwind",
            },
            "BEL": {
                "symbol": "BEL",
                "sector": "defence",
                "signal": "hold",
                "hit_next_day": False,
                "hit_next_week": False,
                "signal_transition": "buy->hold",
                "transition_quality": "unfavorable",
                "transition_beneficial": False,
                "transition_outcome_label": "not-beneficial",
                "is_success": False,
                "is_failure": True,
                "failure_reason": "whipsaw",
                "news_summary": "news: headwind",
                "news_direction": "down",
            },
        },
        "regime_context": {"symbol_count": 10, "avg_effective_intent_score": 54.0, "breadth_above_ema200_pct": 60.0},
        "news_attribution_summaries": [{"symbol": "HAL", "signal": "buy", "summary": "news: tailwind"}],
    }
    text = "\n".join(build_reconcile_digest_lines(summary))
    assert "As-of trade date: 2026-04-12" in text
    assert "Scope: all-stocks" in text
    assert "Failure reasons:" in text
    assert "Signal transition matrix" in text
    assert "beneficial" in text


def test_build_reconcile_digest_lines_handles_insufficient_matured_data():
    summary = {
        "as_of_trade_date": "2026-04-15",
        "scope": "all-stocks",
        "symbol_count": 3,
        "coverage_next_day": 0,
        "coverage_next_week": 0,
        "per_symbol": {
            "HAL": {
                "symbol": "HAL",
                "sector": "defence",
                "signal_transition": "hold->buy",
                "previous_signal": "hold",
                "current_signal": "buy",
                "transition_beneficial": None,
                "transition_outcome_label": "not_evaluable",
                "is_success": False,
                "is_failure": False,
                "news_summary": "news: n/a",
            },
            "BEL": {
                "symbol": "BEL",
                "sector": "defence",
                "signal_transition": "buy->hold",
                "previous_signal": "buy",
                "current_signal": "hold",
                "transition_beneficial": None,
                "transition_outcome_label": "not_evaluable",
                "is_success": False,
                "is_failure": False,
                "news_summary": "news: n/a",
            },
            "BHEL": {
                "symbol": "BHEL",
                "sector": "defence",
                "signal_transition": "hold->hold",
                "previous_signal": "hold",
                "current_signal": "hold",
                "transition_beneficial": True,
                "transition_outcome_label": "beneficial",
                "is_success": True,
                "is_failure": False,
                "news_summary": "news: n/a",
            },
        },
    }
    summary["data_sparse_reason"] = "need_at_least_two_trading_days_with_predictions"
    text = "\n".join(build_reconcile_digest_lines(summary))
    assert "Evaluated: next-day insufficient matured data | next-week insufficient matured data" in text
    assert "Need at least two trading days with next_day_score" in text
    assert "Count: 1 (insufficient matured data)" in text
    assert "Top symbols: none (0 evaluable symbols out of 3)" in text
    assert "HOLD->BUY: insufficient matured data | not-evaluable 1" in text
    assert "BUY->HOLD: insufficient matured data | not-evaluable 1" in text
    assert "HOLD->HOLD" not in text
    assert "No evaluable transition examples (insufficient matured data)." in text


def test_build_reconcile_digest_lines_transition_matrix_only_actual_transitions():
    summary = {
        "coverage_next_day": 2,
        "coverage_next_week": 2,
        "symbol_count": 3,
        "per_symbol": {
            "HAL": {
                "symbol": "HAL",
                "sector": "defence",
                "signal_transition": "hold->buy",
                "previous_signal": "hold",
                "current_signal": "buy",
                "transition_beneficial": True,
                "transition_outcome_label": "beneficial",
                "is_success": True,
                "is_failure": False,
            },
            "BEL": {
                "symbol": "BEL",
                "sector": "defence",
                "signal_transition": "buy->hold",
                "previous_signal": "buy",
                "current_signal": "hold",
                "transition_beneficial": False,
                "transition_outcome_label": "not-beneficial",
                "is_success": False,
                "is_failure": True,
            },
            "BHEL": {
                "symbol": "BHEL",
                "sector": "defence",
                "signal_transition": "hold->hold",
                "previous_signal": "hold",
                "current_signal": "hold",
                "transition_beneficial": True,
                "transition_outcome_label": "beneficial",
                "is_success": True,
                "is_failure": False,
            },
        },
    }
    text = "\n".join(build_reconcile_digest_lines(summary))
    assert "HOLD->BUY: beneficial 1 (100.0%) | not-beneficial 0 (0.0%) | not-evaluable 0" in text
    assert "BUY->HOLD: beneficial 0 (0.0%) | not-beneficial 1 (100.0%) | not-evaluable 0" in text
    assert "HOLD->HOLD" not in text


def test_build_stock_reconcile_snapshot_marks_not_evaluable_for_non_matured_rows():
    table_inputs = {
        "sector": "defence",
        "as_of_trade_date": "2026-04-12",
        "symbol_daily_features": [
            {
                "trade_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "buy",
                "return_1d_pct": 1.0,
                "tape_extras": {"news_correlation": {"direction": "neutral"}},
            },
            {
                "trade_date": "2026-04-11",
                "symbol": "HAL",
                "exchange": "NSE",
                "action_signal": "hold",
                "tape_extras": {},
            },
        ],
        "stock_signal_transition_analytics": [
            {
                "trade_date": "2026-04-12",
                "symbol": "HAL",
                "exchange": "NSE",
                "previous_signal": "hold",
                "current_signal": "buy",
                "transition_type": "hold_to_buy",
                "matured_1w_available": False,
                "matured_1m_available": False,
                "matured_1w_outcome": None,
                "matured_1m_outcome": None,
            }
        ],
        "sector_daily_winners": [],
        "sector_daily_rollup": [],
        "global_news_snapshots": [],
        "llm_digest_memory": [],
    }
    snap = build_stock_reconcile_snapshot([], table_inputs=table_inputs, as_of_trade_date="2026-04-12")
    row = snap["per_symbol"]["HAL"]
    assert row["transition_evaluable"] is False
    assert row["transition_beneficial"] is None
    assert row["transition_outcome_label"] == "not_evaluable"
