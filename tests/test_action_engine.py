"""Tests for action_engine.derive_full_action and digest headlines."""

from __future__ import annotations

import math

import pytest


def _constructive_audit() -> dict:
    return {
        "next_week_score": 72.0,
        "effective_intent_score": 65.0,
        "z_score": 1.0,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 0.5,
        "cmf_20": 0.08,
        "obv_slope_20": 8.0,
        "ema_200_distance_pct": 3.0,
        "ema200_stretch_atr": 1.2,
        "atr_14_pct": 2.0,
        "adx_14": 28.0,
        "fundamental_status": "balanced",
        "rows": 200,
    }


def test_derive_full_action_buy_label():
    from action_engine import derive_full_action

    out = derive_full_action(_constructive_audit())
    assert out["label"] == "buy"
    assert out["label_internal"] == "buy"
    assert out.get("expected_return_5d_pct") is not None
    assert isinstance(out["reasons"], list)
    assert "thresholds_used" in out


def test_derive_full_action_reduce_display_label():
    from action_engine import derive_full_action

    audit = {
        **_constructive_audit(),
        "next_week_score": 40.0,
        "effective_intent_score": 35.0,
        "z_score": -1.5,
        "cmf_20": -0.2,
        "obv_slope_20": -15.0,
        "ema_200_distance_pct": -8.0,
        "return_1d_pct": -3.0,
        "return_5d_pct": -5.0,
        "trap_exit_proxy": True,
        "high_volume_down_day_proxy": True,
    }
    out = derive_full_action(audit)
    assert out["label"] in ("reduce", "exit", "hold")
    if out["label_internal"] == "trim":
        assert out["label"] == "reduce"
    if out["label_internal"] in ("exit", "exit-risk"):
        assert out["label"] == "exit"


def test_digest_headline_uses_reduce_not_trim():
    from action_engine import derive_full_action, digest_headline_text

    audit = {
        **_constructive_audit(),
        "next_week_score": 38.0,
        "effective_intent_score": 32.0,
        "z_score": -2.0,
        "cmf_20": -0.25,
        "obv_slope_20": -20.0,
        "ema_200_distance_pct": -10.0,
        "return_1d_pct": -4.0,
        "return_5d_pct": -6.0,
        "trap_exit_proxy": True,
    }
    action = derive_full_action(audit)
    headline = digest_headline_text(audit, action)
    if action["label"] == "reduce":
        assert headline.startswith("REDUCE")
    elif action["label"] == "exit":
        assert headline.startswith("EXIT")
    else:
        assert "HOLD" in headline or "BUY" in headline or "ACCUMULATE" in headline


def test_position_size_when_calibration_present():
    from action_engine import derive_full_action

    audit = {
        **_constructive_audit(),
        "predicted_probability": 0.72,
        "technical_confidence": 0.85,
    }
    out = derive_full_action(audit)
    assert out["position_size_pct"] is not None
    assert 0.0 < float(out["position_size_pct"]) <= 100.0


def test_expected_drawdown_from_atr():
    from action_engine import derive_full_action

    audit = {**_constructive_audit(), "atr_14_pct": 3.5, "stretch_composite": 12.0}
    out = derive_full_action(audit)
    dd = out.get("expected_drawdown_5d_pct")
    assert dd is not None
    assert 0.5 <= float(dd) <= 15.0


def test_conviction_band_labels():
    from action_engine import conviction_band

    assert conviction_band(25.0) == "low"
    assert conviction_band(46.0) == "moderate"
    assert conviction_band(75.0) == "high"


def test_action_recommendation_digest_lines_plain_english():
    from action_engine import action_recommendation_digest_lines

    audit = {
        **_constructive_audit(),
        "predicted_probability": 0.43,
        "technical_confidence": 0.5,
        "sell_signal": "hold",
        "sell_signal_risk_score": 2.0,
        "next_week_score": 67.27,
        "effective_intent_score": 57.51,
    }
    lines = action_recommendation_digest_lines(audit)
    text = "\n".join(lines)
    assert "Conviction score:" in text
    assert "not a portfolio allocation" in text
    assert "Short-term tilt:" in text
    assert "from ATR" in text
    assert "based on win odds 43%" in text
    assert "Position size:" not in text
    assert "5D outlook:" not in text
    assert "HOLD — risk score" not in text


def test_format_buy_checklist_digest_line_hold_not_buy():
    from action_engine import format_buy_checklist_digest_line

    audit = {
        "sell_signal": "hold",
        "sell_signal_risk_score": 2.0,
        "next_week_score": 67.27,
        "effective_intent_score": 57.51,
    }
    line = format_buy_checklist_digest_line(audit)
    assert line is not None
    assert "Buy checklist:" in line
    assert "67" in line
    assert "✓ passes" in line
    assert "58" in line or "57" in line
    assert "✗ fails" in line
    assert "HOLD not BUY" in line


def test_format_buy_checklist_digest_line_buy_eligible():
    from action_engine import format_buy_checklist_digest_line

    audit = {
        "sell_signal": "hold",
        "sell_signal_risk_score": 1.0,
        "next_week_score": 70.0,
        "effective_intent_score": 62.0,
    }
    line = format_buy_checklist_digest_line(audit)
    assert line is not None
    assert "BUY eligible" in line
