"""Probability calibration layer (always-on production path)."""

from __future__ import annotations


def test_probability_monotonic_in_score():
    from probability_calibration import calibrate_probability

    low = calibrate_probability(48.0)
    mid = calibrate_probability(62.0)
    high = calibrate_probability(78.0)
    assert low < mid < high


def test_probability_bounded():
    from probability_calibration import calibrate_probability

    assert 0.0 <= calibrate_probability(10.0) <= 1.0
    assert 0.0 <= calibrate_probability(95.0) <= 1.0


def test_calibration_improves_brier_vs_raw_confidence():
    from probability_calibration import brier_score, calibrate_probability

    scores = [45.0, 55.0, 65.0, 75.0]
    outcomes = [0, 0, 1, 1]
    raw_conf = [0.5, 0.5, 0.5, 0.5]
    calibrated = [calibrate_probability(s) for s in scores]
    assert brier_score(calibrated, outcomes) < brier_score(raw_conf, outcomes)


def test_calibration_preserves_technical_confidence():
    from probability_calibration import ProbabilityCalibrator, apply_probability_calibration

    audit = {"next_week_score": 67.0, "signal_confidence": 0.55}
    out = apply_probability_calibration(audit)
    assert out["enabled"] is True
    assert audit["technical_confidence"] == 0.55
    assert audit["signal_confidence"] == 0.55
    assert audit["predicted_probability"] == 0.48
    assert audit["predicted_success_probability"] == 0.48
    assert audit["signal_probability"] == 0.48
    assert ProbabilityCalibrator().predict(67.0) == 0.48


def test_exit_risk_bypass_returns_absolute_confidence():
    from probability_calibration import apply_calibration, apply_probability_calibration

    audit = {
        "next_week_score": 40.0,
        "signal_confidence": 0.92,
        "forced_label": "exit-risk",
        "bypass_hysteresis": True,
        "action_signal": "exit-risk",
    }
    assert apply_calibration("exit-risk", 40.0, audit) == 1.0
    apply_probability_calibration(audit)
    assert audit["predicted_probability"] == 1.0
    assert audit["technical_confidence"] == 0.92
    assert audit["signal_confidence"] == 0.92
    assert audit["probability_calibration"]["exit_risk_bypass"] is True


def test_calibration_enabled_by_default():
    from probability_calibration import calibration_enabled

    assert calibration_enabled() is True
