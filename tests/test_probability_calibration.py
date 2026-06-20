"""Phase 6: probability calibration layer."""

from __future__ import annotations

import pytest


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


def test_flag_off_skips_audit_write(monkeypatch):
    from probability_calibration import apply_probability_calibration

    monkeypatch.delenv("TITAN_ENABLE_PROBABILITY_CALIBRATION", raising=False)
    audit = {"next_week_score": 67.0, "signal_confidence": 0.55}
    out = apply_probability_calibration(audit)
    assert out["enabled"] is False
    assert "predicted_probability" not in audit or audit.get("predicted_probability") is None
