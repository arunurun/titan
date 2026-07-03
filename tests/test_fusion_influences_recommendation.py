"""Fusion (titan_score) must materially change predictive scores and action labels."""

from __future__ import annotations

import copy

import pytest


def _pred_audit() -> dict:
    return {
        "effective_intent_score": 60.0,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "rel_return_20d_vs_nifty_pct": 0.5,
        "ema_200_distance_pct": 4.0,
        "atr_14_pct": 1.0,
        "rows": 200,
    }


def _signal_audit() -> dict:
    return {
        "next_week_score": 62.0,
        "effective_intent_score": 58.0,
        "z_score": 0.2,
        "return_1d_pct": 0.5,
        "return_5d_pct": 1.0,
        "return_10d_pct": 2.0,
        "return_21d_pct": 3.0,
        "return_63d_pct": 4.0,
        "return_126d_pct": 5.0,
        "rel_return_5d_vs_nifty_pct": 0.5,
        "cmf_20": 0.05,
        "obv_slope_20": 5.0,
        "ema_200_distance_pct": 2.0,
        "ema200_stretch_atr": 1.2,
        "atr_14_pct": 2.0,
        "adx_14": 24.0,
        "fundamental_status": "neutral",
    }


def test_fusion_pred_blend_high_vs_low_titan_changes_next_week(monkeypatch):
    from prediction_engine import predictive_scores

    high = {**_pred_audit(), "titan_score": 85.0}
    low = {**_pred_audit(), "titan_score": 25.0}
    _, week_high, _ = predictive_scores(high)
    _, week_low, _ = predictive_scores(low)
    assert week_high > week_low

    monkeypatch.setattr("prediction_engine.FUSION_PRED_BLEND", 0.0)
    _, week_high0, _ = predictive_scores(high)
    _, week_low0, _ = predictive_scores(low)
    assert week_high0 == week_low0


def test_fusion_signal_blend_changes_risk_net(monkeypatch):
    import signal_v2 as v2

    high = {**_signal_audit(), "titan_score": 85.0}
    low = {**_signal_audit(), "titan_score": 25.0}
    _, risk_high, _ = v2.evaluate_signal_v2(copy.deepcopy(high))
    _, risk_low, _ = v2.evaluate_signal_v2(copy.deepcopy(low))
    assert risk_high < risk_low

    monkeypatch.setattr(v2, "FUSION_SIGNAL_BLEND", 0.0)
    _, risk_high0, _ = v2.evaluate_signal_v2(copy.deepcopy(high))
    _, risk_low0, _ = v2.evaluate_signal_v2(copy.deepcopy(low))
    assert risk_high0 == risk_low0


def test_refresh_pipeline_sets_action_signal_from_full_scoring(monkeypatch):
    from sector_audit import _refresh_symbol_scoring_outputs

    scores = {
        "technical": {"score": 60.0, "confidence": 0.9, "available": True, "reasons": [], "metadata": {}},
        "relative_strength": {"score": 55.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
        "institutional_flow": {"score": 50.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "fundamentals": {"score": 50.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "market_regime": {"score": 55.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
        "sector_strength": {"score": 52.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "risk": {"score": 60.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
    }
    audit = {
        "factor_scores": copy.deepcopy(scores),
        "effective_intent_score": 60.0,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "rel_return_20d_vs_nifty_pct": 0.5,
        "ema_200_distance_pct": 4.0,
        "atr_14_pct": 1.0,
        "rows": 200,
        "z_score": 0.2,
        "cmf_20": 0.05,
        "adx_14": 24.0,
        "fundamental_status": "neutral",
    }

    def _stub_fusion(a):
        a["titan_score"] = a.pop("_test_titan", 85.0)
        return {"titan_score": a["titan_score"]}

    monkeypatch.setattr("titan_fusion.apply_fusion_to_audit", _stub_fusion)

    high_audit = {**audit, "_test_titan": 85.0}
    _refresh_symbol_scoring_outputs(high_audit)
    high_week = high_audit["next_week_score"]
    high_signal = high_audit["action_signal"]

    low_audit = {**audit, "factor_scores": copy.deepcopy(scores), "_test_titan": 25.0}
    _refresh_symbol_scoring_outputs(low_audit)
    assert low_audit["next_week_score"] < high_week
    assert high_audit["sell_signal"] == high_signal
    assert low_audit["action_signal"]
