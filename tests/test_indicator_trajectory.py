"""Tests for indicator trajectory (Phase 3 primary memory)."""

from __future__ import annotations

import signal_v2 as v2


def _bearish_trajectory(**overrides):
    traj = {
        "window": 5,
        "cmf_slope": -0.04,
        "cmf_positive_sessions": 0,
        "cmf_deteriorating": True,
        "adx_slope": -0.5,
        "adx_strong_bull_sessions": 0,
        "z_elevated_sessions": 0,
        "z_reverting": False,
        "rsi_overbought_sessions": 0,
        "rsi_oversold_sessions": 0,
        "obv_confirm_sessions": 0,
        "flow_tape_score": -0.35,
    }
    traj.update(overrides)
    return traj


def _row(cmf: float, *, z: float = 0.5, adx: float = 22.0, rsi: float = 55.0, obv: bool = True):
    return {
        "z_score": z,
        "tape_extras": {
            "cmf_20": cmf,
            "adx_14": adx,
            "adx_plus_di_14": 28.0,
            "adx_minus_di_14": 14.0,
            "rsi_14": rsi,
            "obv_trend_confirm": obv,
        },
    }


def test_compute_indicator_trajectory_cmf_deteriorating():
    prior = [_row(0.06), _row(0.03), _row(-0.01), _row(-0.04)]
    current = {
        "cmf_20": -0.10,
        "adx_14": 24.0,
        "adx_plus_di_14": 26.0,
        "adx_minus_di_14": 18.0,
        "z_score": 1.1,
        "rsi_14": 58.0,
        "obv_trend_confirm": False,
    }
    traj = v2.compute_indicator_trajectory(prior, current_audit=current, window=5)
    assert traj["window"] == 5
    assert traj["cmf_deteriorating"] is True
    assert traj["cmf_positive_sessions"] == 2
    assert traj["flow_tape_score"] is not None
    assert -1.0 <= float(traj["flow_tape_score"]) <= 1.0


def test_compute_indicator_trajectory_z_reverting():
    prior = [
        _row(0.05, z=2.1),
        _row(0.05, z=2.5),
        _row(0.05, z=2.8),
        _row(0.05, z=3.2),
    ]
    current = {"cmf_20": 0.05, "z_score": 1.5, "rsi_14": 60.0}
    traj = v2.compute_indicator_trajectory(prior, current_audit=current, window=5)
    assert traj["z_elevated_sessions"] >= 3
    assert traj["z_reverting"] is True


def test_trajectory_cmf_deteriorating_caps_ceiling():
    audit = {
        "cmf_20": -0.08,
        "adx_14": 22.0,
        "ema200_stretch_atr": 1.5,
        "indicator_trajectory": {
            "window": 5,
            "cmf_positive_sessions": 0,
            "cmf_deteriorating": True,
            "z_elevated_sessions": 0,
            "z_reverting": False,
            "rsi_overbought_sessions": 0,
        },
    }
    d = v2.layer_d(audit, {"over_extension_hot": False})
    assert d.get("label_ceiling") == "hold"


def test_trajectory_rsi_overbought_hot_stretch_caps_hold():
    audit = {
        "cmf_20": 0.1,
        "adx_14": 28.0,
        "ema200_stretch_atr": 3.0,
        "indicator_trajectory": {
            "window": 5,
            "cmf_positive_sessions": 3,
            "cmf_deteriorating": False,
            "z_elevated_sessions": 1,
            "z_reverting": False,
            "rsi_overbought_sessions": 3,
        },
    }
    c = v2.layer_c(audit)
    d = v2.layer_d(audit, c)
    assert d.get("label_ceiling") == "hold"


def test_prior_streak_requires_bearish_trajectory():
    audit = {
        "prior_constructive_streak": 3,
        "prior_fail_streak": 0,
        "indicator_trajectory": {"flow_tape_score": 0.2, "cmf_deteriorating": False},
    }
    assert v2._prior_session_label_ceiling(audit) is None

    audit["indicator_trajectory"] = _bearish_trajectory()
    assert v2._prior_session_label_ceiling(audit) is None

    audit["prior_fail_streak"] = 2
    assert v2._prior_session_label_ceiling(audit) == "hold"


def test_prior_fail_streak_corroborator_accumulate():
    audit = {
        "prior_constructive_streak": 1,
        "prior_fail_streak": 2,
        "indicator_trajectory": _bearish_trajectory(),
    }
    assert v2._prior_session_label_ceiling(audit) == "accumulate"
