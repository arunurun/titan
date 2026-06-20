"""Probability calibration layer (always-on production path)."""

from __future__ import annotations

import math


def _sample_audit(score: float, *, outcome_up: bool = True, day: int = 0) -> dict:
    return {
        "trade_date": f"2026-05-{day + 1:02d}",
        "next_week_score": score,
        "effective_intent_score": score - 2.0,
        "risk_net": 1.5 if outcome_up else 4.0,
        "sector": "ai",
        "market_regime": {"regime": "BULL"},
        "volume_participation_ratio": 1.4 if outcome_up else 0.8,
        "cmf_20": 0.12 if outcome_up else -0.08,
    }


def _labeled_cohort(n: int = 40) -> tuple[list[dict], list[int]]:
    rows: list[dict] = []
    outcomes: list[int] = []
    for i in range(n):
        up = i % 2 == 0
        score = 52.0 + (i % 20) * 1.5
        rows.append(_sample_audit(score, outcome_up=up, day=i))
        outcomes.append(1 if up else 0)
    return rows, outcomes


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

    audit = {"next_week_score": 70.0, "signal_confidence": 0.55}
    out = apply_probability_calibration(audit)
    assert out["enabled"] is True
    assert out["method"] == "bucket"
    assert audit["technical_confidence"] == 0.55
    assert audit["signal_confidence"] == 0.55
    assert audit["predicted_probability"] == 0.48
    assert audit["predicted_success_probability"] == 0.48
    assert audit["signal_probability"] == 0.48
    assert audit["position_confidence"] == 0.6 * 0.48 + 0.4 * 0.55
    assert audit["position_score"] == audit["position_confidence"]
    assert ProbabilityCalibrator().predict(70.0) == 0.48


def test_enforce_mode_never_overwrites_signal_confidence():
    """Production enforce path must leave engine signal_confidence untouched."""
    from probability_calibration import IsotonicCalibrator, ProbabilityCalibrator, apply_probability_calibration

    original_conf = 0.73
    audit = {"next_week_score": 70.0, "signal_confidence": original_conf}
    apply_probability_calibration(audit)
    assert audit["signal_confidence"] == original_conf
    assert audit["technical_confidence"] == original_conf
    assert audit["predicted_probability"] != original_conf

    rows, outcomes = _labeled_cohort(35)
    iso = IsotonicCalibrator(min_train=30)
    iso.fit_walk_forward(rows, outcomes)
    cal = ProbabilityCalibrator(isotonic=iso)
    audit_iso = dict(rows[20])
    audit_iso["signal_confidence"] = original_conf
    apply_probability_calibration(audit_iso, calibrator=cal)
    assert audit_iso["signal_confidence"] == original_conf
    assert audit_iso["technical_confidence"] == original_conf


def test_compute_position_score():
    from probability_calibration import compute_position_confidence, compute_position_score

    assert compute_position_score(0.6, 0.4) == 0.52
    assert compute_position_confidence(0.6, 0.4) == 0.52
    assert compute_position_score(None, 0.7) == 0.7


def test_extract_isotonic_features():
    from probability_calibration import extract_isotonic_features

    audit = {
        "next_week_score": 68.0,
        "effective_intent_score": 66.0,
        "sell_signal_risk_score": 2.1,
        "sector_key": "telecom",
        "market_regime": {"regime": "STRONG_BULL"},
        "volume_participation_ratio": 1.8,
        "cmf_20": 0.15,
    }
    feats = extract_isotonic_features(audit)
    assert feats["next_week_score"] == 68.0
    assert feats["effective_intent_score"] == 66.0
    assert feats["risk_net"] == 2.1
    assert feats["sector"] == "telecom"
    assert feats["market_regime"] == "STRONG_BULL"
    assert feats["volume_participation_ratio"] == 1.8
    assert feats["cmf_20"] == 0.15


def test_isotonic_fit_and_predict():
    from probability_calibration import IsotonicCalibrator

    rows, outcomes = _labeled_cohort(40)
    cal = IsotonicCalibrator(min_train=30)
    assert cal.fit(rows, outcomes) is True
    assert cal.is_fitted
    assert cal.n_train >= 30

    low = cal.predict(_sample_audit(52.0, outcome_up=False))
    high = cal.predict(_sample_audit(82.0, outcome_up=True))
    assert not math.isnan(low)
    assert not math.isnan(high)
    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0
    assert low <= high


def test_isotonic_not_fitted_returns_nan():
    from probability_calibration import IsotonicCalibrator

    cal = IsotonicCalibrator()
    assert cal.is_fitted is False
    assert math.isnan(cal.predict({"next_week_score": 60.0}))


def test_isotonic_fallback_when_insufficient_data():
    from probability_calibration import IsotonicCalibrator, ProbabilityCalibrator, apply_probability_calibration

    rows, outcomes = _labeled_cohort(10)
    iso = IsotonicCalibrator(min_train=30)
    assert iso.fit(rows, outcomes) is False
    cal = ProbabilityCalibrator(isotonic=iso)
    assert cal.calibration_method() == "bucket"

    audit = {"next_week_score": 70.0, "signal_confidence": 0.55}
    out = apply_probability_calibration(audit, calibrator=cal)
    assert out["method"] == "bucket"
    assert out["isotonic_trained"] is False
    assert audit["predicted_probability"] == 0.48


def test_walk_forward_fit_succeeds():
    from probability_calibration import IsotonicCalibrator

    rows, outcomes = _labeled_cohort(45)
    cal = IsotonicCalibrator(min_train=30)
    assert cal.fit_walk_forward(rows, outcomes) is True
    assert cal.is_fitted
    prob = cal.predict(rows[-1])
    assert not math.isnan(prob)


def test_apply_uses_isotonic_when_trained():
    from probability_calibration import IsotonicCalibrator, ProbabilityCalibrator, apply_probability_calibration

    rows, outcomes = _labeled_cohort(35)
    iso = IsotonicCalibrator(min_train=30)
    iso.fit_walk_forward(rows, outcomes)
    cal = ProbabilityCalibrator(isotonic=iso)

    audit = dict(rows[20])
    audit["signal_confidence"] = 0.62
    out = apply_probability_calibration(audit, calibrator=cal)
    assert out["method"] == "isotonic"
    assert out["isotonic_trained"] is True
    assert out["isotonic_n_train"] >= 30
    assert audit["technical_confidence"] == 0.62
    assert audit["signal_confidence"] == 0.62
    assert audit["predicted_probability"] is not None

    bucket_audit = dict(audit)
    bucket_out = apply_probability_calibration(bucket_audit, calibrator=ProbabilityCalibrator())
    assert bucket_out["method"] == "bucket"


def test_isotonic_improves_brier_on_synthetic_cohort():
    from probability_calibration import IsotonicCalibrator, brier_score, calibrate_probability

    rows: list[dict] = []
    outcomes: list[int] = []
    for i in range(50):
        score = 48.0 + i * 0.8
        up = score >= 68.0
        rows.append(_sample_audit(score, outcome_up=up, day=i))
        outcomes.append(1 if up else 0)

    bucket_preds = [calibrate_probability(r["next_week_score"]) for r in rows]
    iso = IsotonicCalibrator(min_train=30)
    iso.fit(rows, outcomes)
    iso_preds = [iso.predict(r) for r in rows]

    assert brier_score(iso_preds, outcomes) <= brier_score(bucket_preds, outcomes)


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
    from probability_calibration import calibration_enabled, calibration_mode

    assert calibration_enabled() is True
    assert calibration_mode() == "enforce"
