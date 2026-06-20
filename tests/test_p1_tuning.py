"""P1 reconciliation bundle: AI tighten, mid-band accumulate, Tier-2 momentum, overext damp."""

from __future__ import annotations

import pytest

import signal_v2 as v2
from sector_priority import enrich_audit_sector_signal_profile, sector_signal_profile_for


def _base_audit(**overrides):
    audit = {
        "next_week_score": 62.0,
        "effective_intent_score": 58.0,
        "z_score": 0.8,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.08,
        "ema_200_distance_pct": 8.0,
        "ema200_stretch_atr": 2.0,
        "atr_14_pct": 2.5,
        "adx_14": 28.0,
        "adx_plus_di_14": 30.0,
        "adx_minus_di_14": 12.0,
        "volume_participation_ratio": 1.4,
    }
    audit.update(overrides)
    return audit


def test_ai_sector_profile_tightens_buy_and_accumulate():
    profile = sector_signal_profile_for("ai")
    assert profile["buy_nw_min"] == 68.0
    assert profile["buy_intent_min"] == 63.0
    assert profile["cmf_constructive_min"] == 0.0

    audit = _base_audit(
        sector_key="ai",
        next_week_score=66.0,
        effective_intent_score=61.0,
        cmf_20=0.02,
        volume_participation_ratio=1.5,
    )
    enrich_audit_sector_signal_profile(audit, "ai")
    gate = v2._buy_gate(audit, {"buy_allowed": True}, {"over_extension_hot": False})
    assert gate["constructive_scores"] is False
    assert v2._accumulate_band(audit, 2.5, buy_allowed=True) is False


def test_ai_sector_blocks_marginal_constructive_e2e():
    """AI false-positive class: marginal scores + weak CMF should not reach accumulate."""
    audit = _base_audit(
        sector_key="ai",
        next_week_score=61.0,
        effective_intent_score=61.0,
        cmf_20=-0.02,
        volume_participation_ratio=1.2,
        ema200_stretch_atr=1.5,
    )
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "trim")


def test_mid_band_accumulate_nelco_class():
    """NELCO-class: nw/intent 55-64 with VPR support -> accumulate."""
    audit = _base_audit(
        next_week_score=56.0,
        effective_intent_score=57.0,
        volume_participation_ratio=1.35,
        cmf_20=0.05,
    )
    assert v2._mid_band_accumulate_ok(audit, 2.5, buy_allowed=True)
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < v2._buy_risk_ceiling()
    assert label == "accumulate"


def test_mid_band_requires_participation():
    audit = _base_audit(
        next_week_score=56.0,
        effective_intent_score=57.0,
        volume_participation_ratio=0.8,
        cmf_20=-0.01,
    )
    assert v2._mid_band_accumulate_ok(audit, 2.5, buy_allowed=True) is False


def test_momentum_tier2_rally_deescalates_trim():
    """Momentum sector: Tier-2 trim on rally tape may de-escalate to accumulate."""
    audit = _base_audit(
        sector_key="telecom",
        next_week_score=62.0,
        effective_intent_score=68.0,
        return_1d_pct=2.5,
        return_5d_pct=4.0,
        cmf_20=-0.08,
        ema200_stretch_atr=4.5,
        ema_200_distance_pct=16.0,
        z_score=2.6,
        volume_participation_ratio=1.5,
        prev_action_signal="trim",
    )
    label, risk, reasons = v2.evaluate_signal_v2(audit)
    assert risk < 5.0
    assert label in ("accumulate", "hold")
    assert label != "trim"


def test_strong_rally_overext_skips_tier2_corroborator():
    from signal_v2 import layer_b

    c = {"over_extension_hot": True, "families": {}, "trace": []}
    audit = _base_audit(
        cmf_20=0.12,
        adx_14=30.0,
        adx_plus_di_14=28.0,
        adx_minus_di_14=10.0,
        rel_return_5d_vs_nifty_pct=2.0,
    )
    b = layer_b(audit, c, {"staleflow_downgrade": False})
    assert b["forced_label"] is None


def test_overextension_ceiling_damp_default():
    assert v2._overextension_ceiling_mode() == "damp"


def test_overextension_hold_damp_to_accumulate_on_rally():
    audit = _base_audit(
        ema200_stretch_atr=6.5,
        z_score=2.8,
        ema_200_distance_pct=20.0,
        breakout_20d_distance_pct_to_high=-0.5,
        cmf_20=0.1,
    )
    oe = v2._overextension_ceiling(audit)
    assert oe["ceiling"] == "hold"
    applied = v2._resolve_overext_applied_ceiling(audit, oe["ceiling"], "damp")
    assert applied == "accumulate"


def test_overextension_enforce_rally_damp_hold_not_hard():
    audit = _base_audit(
        ema200_stretch_atr=6.5,
        cmf_20=0.1,
    )
    applied = v2._resolve_overext_applied_ceiling(audit, "hold", "enforce")
    assert applied == "accumulate"
