"""Tests for FUSION_PRED_BLEND in prediction_engine."""

from __future__ import annotations

import pytest


@pytest.fixture
def base_audit() -> dict:
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
        "titan_score": 80.0,
    }


def test_pred_blend_default_on(base_audit: dict):
    from prediction_engine import FUSION_PRED_BLEND, predictive_scores

    assert FUSION_PRED_BLEND == pytest.approx(0.15)
    day, week, bd = predictive_scores(base_audit)
    audit_no_titan = dict(base_audit)
    audit_no_titan.pop("titan_score")
    day2, week2, _ = predictive_scores(audit_no_titan)
    assert day != day2
    assert week != week2
    assert bd["titan_fusion_pred_blend"] == pytest.approx(0.15)


def test_pred_blend_moves_toward_titan(base_audit: dict, monkeypatch):
    from prediction_engine import FUSION_PRED_BLEND, predictive_scores

    day_blend, week_blend, bd = predictive_scores(base_audit)
    monkeypatch.setattr("prediction_engine.FUSION_PRED_BLEND", 0.0)
    day_base, week_base, _ = predictive_scores(base_audit)
    assert day_blend > day_base
    assert week_blend > week_base
    assert bd["titan_fusion_pred_blend"] == pytest.approx(FUSION_PRED_BLEND)
