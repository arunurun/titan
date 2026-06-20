"""Medium-term momentum horizons (5d/21d/63d/126d) in signal_v2 Layer C."""

from __future__ import annotations

import signal_v2 as v2


def test_63d_leader_scores_higher_momentum_risk_when_weak():
    weak_leader = v2._family_points(
        {
            "return_5d_pct": -1.0,
            "return_21d_pct": -5.0,
            "return_63d_pct": -15.0,
            "return_126d_pct": -22.0,
            "next_week_score": 60.0,
            "effective_intent_score": 55.0,
            "z_score": 0.0,
            "ema_200_distance_pct": 2.0,
            "atr_14_pct": 2.0,
        }
    )
    mild = v2._family_points(
        {
            "return_5d_pct": -1.0,
            "return_21d_pct": -1.0,
            "return_63d_pct": -2.0,
            "return_126d_pct": -3.0,
            "next_week_score": 60.0,
            "effective_intent_score": 55.0,
            "z_score": 0.0,
            "ema_200_distance_pct": 2.0,
            "atr_14_pct": 2.0,
        }
    )
    assert weak_leader["families"]["momentum"] > mild["families"]["momentum"]


def test_one_day_spike_alone_does_not_dominate():
    spike = v2._family_points(
        {
            "return_1d_pct": -3.0,
            "return_5d_pct": 0.5,
            "return_21d_pct": 4.0,
            "return_63d_pct": 8.0,
            "return_126d_pct": 12.0,
            "next_week_score": 70.0,
            "effective_intent_score": 68.0,
            "z_score": 0.5,
            "ema_200_distance_pct": 3.0,
            "atr_14_pct": 2.0,
        }
    )
    assert spike["families"]["momentum"] < 1.5


def test_126d_weakness_contributes_to_composite():
    base = {
        "return_5d_pct": -0.5,
        "return_21d_pct": -0.5,
        "return_63d_pct": -1.0,
        "return_126d_pct": -2.0,
        "next_week_score": 60.0,
        "effective_intent_score": 55.0,
        "z_score": 0.0,
        "ema_200_distance_pct": 2.0,
        "atr_14_pct": 2.0,
    }
    mild = v2._family_points(base)
    weak_126 = v2._family_points({**base, "return_126d_pct": -25.0})
    assert weak_126["families"]["momentum"] > mild["families"]["momentum"]
    assert "return_5d/21d/63d/126d_pct" in weak_126["trace"][0]["metric"]


def test_momentum_weights_favor_medium_term_horizons():
    """63d and 126d ramps dominate over 5d when only one horizon is weak."""
    only_5d_weak = v2._family_points(
        {
            "return_5d_pct": -6.0,
            "return_21d_pct": 2.0,
            "return_63d_pct": 5.0,
            "return_126d_pct": 8.0,
            "next_week_score": 70.0,
            "effective_intent_score": 68.0,
            "z_score": 0.0,
            "ema_200_distance_pct": 3.0,
            "atr_14_pct": 2.0,
        }
    )
    only_126_weak = v2._family_points(
        {
            "return_5d_pct": 2.0,
            "return_21d_pct": 2.0,
            "return_63d_pct": 5.0,
            "return_126d_pct": -30.0,
            "next_week_score": 70.0,
            "effective_intent_score": 68.0,
            "z_score": 0.0,
            "ema_200_distance_pct": 3.0,
            "atr_14_pct": 2.0,
        }
    )
    assert only_126_weak["families"]["momentum"] > only_5d_weak["families"]["momentum"]
