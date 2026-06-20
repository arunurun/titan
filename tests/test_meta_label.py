"""Phase 3: LightGBM meta-label BUY veto filter."""

from __future__ import annotations

import math

import pytest


def _strong_buy_audit(**overrides) -> dict:
    audit = {
        "next_week_score": 78.0,
        "effective_intent_score": 72.0,
        "cmf_20": 0.12,
        "volume_participation_ratio": 1.25,
        "sector_relative_rank_score": 68.0,
        "market_regime": {"regime": "BULL"},
    }
    audit.update(overrides)
    return audit


def _weak_buy_audit(**overrides) -> dict:
    audit = {
        "next_week_score": 66.0,
        "effective_intent_score": 60.0,
        "cmf_20": -0.05,
        "volume_participation_ratio": 0.85,
        "sector_pctile_next_week_score": 32.0,
        "market_regime": {"regime": "DEFENSIVE"},
    }
    audit.update(overrides)
    return audit


def test_extract_features_keys():
    from meta_label import FEATURE_KEYS, extract_features

    feats = extract_features(_strong_buy_audit(), risk_net=1.2)
    assert set(feats.keys()) == set(FEATURE_KEYS)
    assert feats["regime"] == "BULL"
    assert feats["risk_net"] == 1.2
    assert feats["sector_rank"] == 68.0


def test_rule_fallback_vetoes_weak_buy():
    from meta_label import apply_meta_label_veto, rule_based_veto, extract_features

    audit = _weak_buy_audit()
    feats = extract_features(audit, risk_net=2.6)
    veto, reasons = rule_based_veto(feats)
    assert veto is True
    assert len(reasons) >= 2

    out = apply_meta_label_veto("buy", audit, risk_net=2.6)
    assert out == "accumulate"
    assert audit["meta_label"]["applied"] is True
    assert audit["meta_label"]["method"] == "rule_fallback"


def test_rule_fallback_passes_strong_buy():
    from meta_label import apply_meta_label_veto, rule_based_veto, extract_features

    audit = _strong_buy_audit()
    feats = extract_features(audit, risk_net=0.8)
    veto, reasons = rule_based_veto(feats)
    assert veto is False
    assert "strong_conviction_pass" in reasons

    out = apply_meta_label_veto("buy", audit, risk_net=0.8)
    assert out == "buy"
    assert audit["meta_label"]["applied"] is False


def test_non_buy_labels_untouched():
    from meta_label import apply_meta_label_veto

    for label in ("accumulate", "hold", "trim", "exit-risk"):
        audit = _weak_buy_audit()
        out = apply_meta_label_veto(label, audit, risk_net=2.6)
        assert out == label
        assert audit["meta_label"]["applied"] is False
        assert audit["meta_label"]["reason"] == "not_buy"


def test_lightgbm_missing_graceful():
    from meta_label import MetaLabelModel, lightgbm_available

    model = MetaLabelModel()
    rows = [_strong_buy_audit() for _ in range(40)]
    outcomes = [1] * 20 + [0] * 20
    trained = model.fit([{**r, "risk_net": 1.0} for r in rows], outcomes)
    if lightgbm_available():
        assert trained is True
    else:
        assert trained is False
        veto, reason, detail = model.should_veto({"risk_net": 1.0, "regime": "NEUTRAL"})
        assert detail["method"] == "rule_fallback"
        assert isinstance(veto, bool)
        assert reason


def test_lightgbm_fit_requires_cohort():
    from meta_label import MetaLabelModel

    model = MetaLabelModel()
    assert model.fit([{"risk_net": 1.0}], [1]) is False
    assert model.fitted is False


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("lightgbm"),
    reason="lightgbm optional dependency not installed",
)
def test_lightgbm_model_veto_when_fitted():
    from meta_label import MetaLabelModel, extract_features

    model = MetaLabelModel(threshold=0.55)
    rows = []
    outcomes = []
    for i in range(35):
        audit = _strong_buy_audit(next_week_score=80.0 - i * 0.5, cmf_20=0.15 - i * 0.01)
        feats = extract_features(audit, risk_net=0.5 + i * 0.05)
        rows.append(feats)
        outcomes.append(1 if i < 18 else 0)
    assert model.fit(rows, outcomes) is True

    weak = extract_features(_weak_buy_audit(), risk_net=2.8)
    veto, reason, detail = model.should_veto(weak)
    assert detail["method"] == "lightgbm"
    assert "pass_probability" in detail
    assert isinstance(veto, bool)
    assert reason in ("lgbm_low_confidence", "lgbm_pass")


def test_signal_v2_wires_meta_label_after_resolution(monkeypatch):
    import signal_v2 as v2
    from meta_label import apply_meta_label_veto

    calls: list[tuple[str, float]] = []

    def _spy(label, audit, *, risk_net):
        calls.append((label, risk_net))
        return apply_meta_label_veto(label, audit, risk_net=risk_net)

    monkeypatch.setattr("meta_label.apply_meta_label_veto", _spy)

    audit = _strong_buy_audit(
        return_1d_pct=2.0,
        return_5d_pct=2.0,
        return_10d_pct=3.0,
        return_21d_pct=4.0,
        return_63d_pct=6.0,
        return_126d_pct=8.0,
        z_score=1.5,
        obv_slope_20=10.0,
        ema_200_distance_pct=3.0,
        ema200_stretch_atr=1.5,
        atr_14_pct=2.0,
        adx_14=30.0,
        fundamental_status="strong",
    )
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("buy", "accumulate", "hold", "trim", "exit-risk")
    if label == "buy" or (audit.get("meta_label") or {}).get("label_in") == "buy":
        assert calls, "meta-label veto should run after label resolution"
        assert isinstance(risk, float)


def test_vpr_fallback_keys():
    from meta_label import extract_features

    audit = _strong_buy_audit()
    audit.pop("volume_participation_ratio", None)
    audit["absorption_for_scoring"] = 1.15
    feats = extract_features(audit, risk_net=1.0)
    assert feats["volume_participation_ratio"] == pytest.approx(1.15)


def test_nan_features_rule_still_runs():
    from meta_label import apply_meta_label_veto

    audit = {"market_regime": {"regime": "NEUTRAL"}}
    out = apply_meta_label_veto("buy", audit, risk_net=float("nan"))
    assert out in ("buy", "accumulate")
    assert "meta_label" in audit
