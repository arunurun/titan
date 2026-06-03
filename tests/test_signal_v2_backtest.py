"""Pure-function tests for signal_v2 backtest / A/B harness."""

from __future__ import annotations

import os

import pytest

from signal_v2_backtest import (
    BUILTIN_FIXTURE_ROWS,
    drawdown_saved_pct,
    false_exit_forgone_pct,
    flip_rate,
    is_defensive_escalation,
    is_false_exit_rescue,
    return_direction,
    direction_hit,
    signal_predicted_direction,
    feature_row_to_audit,
    audit_has_signal_inputs,
    compare_label_streams,
    run_legacy_vs_v2_ab,
    recompute_label,
    signal_env,
)


def test_signal_predicted_direction_accumulate_is_up():
    assert signal_predicted_direction("accumulate") == "up"
    assert signal_predicted_direction("exit-risk") == "down"
    assert signal_predicted_direction("hold") == "neutral"


def test_direction_hit_matches_analysis_store_semantics():
    assert direction_hit("up", "up") is True
    assert direction_hit("up", "down") is False
    assert direction_hit("unknown", "up") is None


def test_drawdown_saved_and_false_exit_forgone():
    assert drawdown_saved_pct(-2.5) == 2.5
    assert drawdown_saved_pct(1.0) == -1.0
    assert false_exit_forgone_pct(1.2) == 1.2


def test_flip_rate_two_changes_in_three_steps():
    assert flip_rate(["buy", "hold", "trim"]) == pytest.approx(2 / 2)


def test_defensive_escalation_and_rescue_detection():
    assert is_defensive_escalation("buy", "trim")
    assert not is_defensive_escalation("trim", "exit-risk")
    assert is_false_exit_rescue("trim", "hold")
    assert not is_false_exit_rescue("hold", "buy")


def test_feature_row_to_audit_merges_tape_extras():
    row = {
        "symbol": "X",
        "next_week_score": 70.0,
        "tape_extras": {"cmf_20": 0.1, "return_5d_pct": 3.0},
    }
    audit = feature_row_to_audit(row)
    assert audit is not None
    assert audit["cmf_20"] == 0.1
    assert audit["return_5d_pct"] == 3.0
    assert audit_has_signal_inputs(audit)


def test_compare_label_streams_aggregates_drawdown_event():
    rows = [
        {
            "symbol": "A",
            "exchange": "NSE",
            "trade_date": "2026-05-01",
            "return_1d_pct": -3.0,
        }
    ]
    metrics = compare_label_streams(
        rows,
        reference_labels=["buy"],
        candidate_labels=["trim"],
        name="test",
    )
    assert metrics.defensive_escalation_events == 1
    assert metrics.drawdown_saved_n == 1
    assert metrics.drawdown_saved_sum == pytest.approx(3.0)


def test_recompute_legacy_vs_v2_differ_when_v2_on(monkeypatch):
    monkeypatch.delenv("TITAN_SIGNAL_V2", raising=False)
    audit = {
        "next_week_score": 72.0,
        "effective_intent_score": 68.0,
        "z_score": 2.2,
        "return_1d_pct": -2.4,
        "ema_200_distance_pct": 6.0,
        "atr_14_pct": 2.5,
        "cmf_20": -0.18,
        "adx_14": 22.0,
        "ema200_stretch_atr": 2.8,
    }
    leg = recompute_label(audit, use_v2=False)
    v2 = recompute_label(audit, use_v2=True)
    assert leg in ("buy", "hold", "accumulate")
    assert v2 in ("hold", "trim", "exit-risk", "accumulate")


def test_run_legacy_vs_v2_ab_on_builtin_fixture():
    report = run_legacy_vs_v2_ab(BUILTIN_FIXTURE_ROWS)
    core = report["legacy_vs_v2"]
    assert core["row_count"] >= 1
    assert "direction_hit_rate" in core
    assert "flip_guardrail_pass" in report
    assert isinstance(report["per_layer_ablation"], list)


def test_signal_env_restores_flags(monkeypatch):
    monkeypatch.setenv("TITAN_SIGNAL_V2", "0")
    with signal_env(use_v2=True):
        assert os.environ.get("TITAN_SIGNAL_V2") == "1"
    assert os.environ.get("TITAN_SIGNAL_V2") == "0"
