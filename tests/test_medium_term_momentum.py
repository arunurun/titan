"""Medium-term momentum horizons (21d/63d) in signal_v2 Layer C."""

from __future__ import annotations

import pytest

import signal_v2 as v2


@pytest.fixture(autouse=True)
def _enforce_momentum(monkeypatch):
    monkeypatch.setenv("TITAN_MEDIUM_TERM_MOMENTUM", "1")
    monkeypatch.setenv("TITAN_MEDIUM_TERM_MOMENTUM_MODE", "enforce")


def test_63d_leader_scores_higher_momentum_risk_when_weak():
    weak_leader = v2._family_points(
        {
            "return_1d_pct": -0.5,
            "return_5d_pct": -1.0,
            "return_21d_pct": -5.0,
            "return_63d_pct": -15.0,
            "next_week_score": 60.0,
            "effective_intent_score": 55.0,
            "z_score": 0.0,
            "ema_200_distance_pct": 2.0,
            "atr_14_pct": 2.0,
        }
    )
    mild = v2._family_points(
        {
            "return_1d_pct": -0.5,
            "return_5d_pct": -1.0,
            "return_21d_pct": -1.0,
            "return_63d_pct": -2.0,
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
            "next_week_score": 70.0,
            "effective_intent_score": 68.0,
            "z_score": 0.5,
            "ema_200_distance_pct": 3.0,
            "atr_14_pct": 2.0,
        }
    )
    assert spike["families"]["momentum"] < 1.5


def test_extreme_move_downweights_1d_only():
    base = {
        "return_1d_pct": -2.0,
        "return_5d_pct": -2.0,
        "return_21d_pct": -3.0,
        "return_63d_pct": -4.0,
        "next_week_score": 60.0,
        "effective_intent_score": 55.0,
        "z_score": 0.0,
        "ema_200_distance_pct": 2.0,
        "atr_14_pct": 2.0,
    }
    normal = v2._family_points(base)
    extreme = v2._family_points({**base, "extreme_price_move_proxy": True})
    assert extreme["families"]["momentum"] <= normal["families"]["momentum"]


def test_legacy_mode_uses_10d_horizon(monkeypatch):
    monkeypatch.delenv("TITAN_MEDIUM_TERM_MOMENTUM", raising=False)
    audit = {
        "return_1d_pct": -0.5,
        "return_5d_pct": -1.0,
        "return_10d_pct": -8.0,
        "return_21d_pct": -1.0,
        "return_63d_pct": -2.0,
        "next_week_score": 60.0,
        "effective_intent_score": 55.0,
        "z_score": 0.0,
        "ema_200_distance_pct": 2.0,
        "atr_14_pct": 2.0,
    }
    legacy = v2._family_points(audit)
    assert "return_1d/5d/10d_pct" in legacy["trace"][0]["metric"]
