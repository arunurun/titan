"""Parity: prediction_engine.predictive_scores vs sector_audit._predictive_scores."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_audit() -> dict:
    return {
        "effective_intent_score": 62.0,
        "return_1d_pct": 1.5,
        "return_5d_pct": 3.0,
        "return_10d_pct": 4.5,
        "rel_return_5d_vs_nifty_pct": 2.0,
        "rel_return_20d_vs_nifty_pct": 1.0,
        "ema_200_distance_pct": 5.0,
        "atr_penalty_input": 1.2,
        "rows": 220,
        "extreme_price_move_proxy": False,
        "trap_exit_proxy": False,
        "high_volume_down_day_proxy": False,
        "event_risk_soon": False,
    }


def test_predictive_scores_parity(sample_audit: dict):
    from prediction_engine import predictive_scores
    from sector_audit import _predictive_scores

    day_a, week_a, bd_a = predictive_scores(sample_audit)
    day_b, week_b, bd_b = _predictive_scores(dict(sample_audit))
    assert day_a == day_b
    assert week_a == week_b
    assert bd_a == bd_b


def test_predictive_scores_with_penalties(sample_audit: dict):
    from prediction_engine import predictive_scores
    from sector_audit import _predictive_scores

    audit = dict(sample_audit)
    audit["trap_exit_proxy"] = True
    day_a, week_a, _ = predictive_scores(audit)
    day_b, week_b, _ = _predictive_scores(audit)
    assert day_a == day_b
    assert week_a == week_b
    assert day_a < predictive_scores(sample_audit)[0]
