"""Regression tests for weekly backtest gap fixes (AARTIIND / HEROMOTOCO / MOTHERSON)."""

from __future__ import annotations

import signal_v2 as v2


def _base_audit(**overrides):
    audit = {
        "next_week_score": 70.0,
        "effective_intent_score": 68.0,
        "z_score": 1.2,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "rel_return_5d_vs_nifty_pct": 2.0,
        "cmf_20": 0.08,
        "ema_200_distance_pct": 8.0,
        "ema200_stretch_atr": 2.0,
        "atr_14_pct": 2.5,
        "adx_14": 28.0,
        "adx_plus_di_14": 30.0,
        "adx_minus_di_14": 12.0,
        "volume_participation_ratio": 1.4,
        "obv_trend_confirm": True,
    }
    audit.update(overrides)
    return audit


def test_cmf_divergence_blocks_clean_buy():
    """HEROMOTOCO-class: negative CMF + hollow breakout blocks clean buy."""
    audit = _base_audit(
        cmf_20=-0.12,
        return_1d_pct=3.0,
        obv_trend_confirm=False,
        next_week_score=72.0,
        effective_intent_score=66.0,
    )
    c = v2.layer_c(audit)
    gate = v2._buy_gate(audit, {"buy_allowed": True}, c)
    assert v2._divergence_bear_proxy(audit)
    assert gate["clean_buy"] is False


def test_chase_block_without_rel_accel():
    """AARTIIND-class: +8% 5d rally blocks buy without rel-strength acceleration."""
    audit = _base_audit(
        return_5d_pct=15.2,
        rel_return_5d_vs_nifty_pct=2.0,
        prev_rel_return_5d_vs_nifty_pct=3.5,
        next_week_score=71.0,
        effective_intent_score=69.0,
        cmf_20=0.51,
        ema200_stretch_atr=2.8,
    )
    c = v2.layer_c(audit)
    gate = v2._buy_gate(audit, {"buy_allowed": True}, c)
    assert v2._post_rally_chase_block(audit)
    assert gate["clean_buy"] is False
    assert gate["constructive_core"] is False
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label != "buy"


def test_chase_allowed_when_rel_accel_and_pullback():
    audit = _base_audit(
        return_5d_pct=10.0,
        rel_return_5d_vs_nifty_pct=6.0,
        prev_rel_return_5d_vs_nifty_pct=3.0,
        return_1d_pct=-1.0,
        volume_participation_ratio=0.8,
        cmf_20=0.08,
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    assert v2._rel_strength_accelerating(audit)
    assert v2._pullback_quality_proxy(audit)
    assert not v2._post_rally_chase_block(audit)


def test_chase_blocked_when_rel_accel_without_pullback():
    audit = _base_audit(
        return_5d_pct=10.0,
        rel_return_5d_vs_nifty_pct=6.0,
        prev_rel_return_5d_vs_nifty_pct=3.0,
        return_1d_pct=1.5,
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    assert v2._rel_strength_accelerating(audit)
    assert not v2._pullback_quality_proxy(audit)
    assert v2._post_rally_chase_block(audit)


def test_cmf_between_neg005_and_neg010_blocks_core():
    """WIPRO-class: CMF below buy floor blocks constructive_core."""
    audit = _base_audit(
        cmf_20=-0.08,
        return_1d_pct=0.5,
        next_week_score=64.0,
        effective_intent_score=66.0,
    )
    gate = v2._buy_gate(audit, {"buy_allowed": True}, {"over_extension_hot": False})
    assert gate["constructive_core"] is False


def test_extreme_stretch_always_hold():
    audit = _base_audit(
        ema200_stretch_atr=7.5,
        cmf_20=0.2,
        sector_pctile_effective_intent=90.0,
        adx_14=30.0,
    )
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "hold"


def test_prior_constructive_streak_caps_hold():
    audit = _base_audit(
        prior_constructive_streak=3,
        prior_fail_streak=2,
        indicator_trajectory={
            "window": 5,
            "cmf_deteriorating": True,
            "cmf_positive_sessions": 0,
            "flow_tape_score": -0.3,
            "z_elevated_sessions": 0,
            "z_reverting": False,
        },
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    d = v2.layer_d(audit, {"over_extension_hot": False})
    assert d.get("label_ceiling") == "hold"
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "hold"


def test_prior_streak_ignored_without_bearish_trajectory():
    audit = _base_audit(
        prior_constructive_streak=3,
        prior_fail_streak=2,
        indicator_trajectory={"flow_tape_score": 0.25, "cmf_deteriorating": False},
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    assert v2._prior_session_label_ceiling(audit) is None


def test_prior_fail_streak_caps_accumulate():
    audit = _base_audit(
        prior_constructive_streak=2,
        prior_fail_streak=2,
        indicator_trajectory={
            "window": 5,
            "cmf_deteriorating": True,
            "cmf_positive_sessions": 1,
            "flow_tape_score": -0.25,
            "z_elevated_sessions": 0,
            "z_reverting": False,
        },
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    d = v2.layer_d(audit, {"over_extension_hot": False})
    assert d.get("label_ceiling") == "accumulate"
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "accumulate")
    assert label != "buy"


def test_sector_leader_skips_chase_block():
    audit = _base_audit(
        return_5d_pct=12.0,
        rel_return_5d_vs_nifty_pct=2.0,
        prev_rel_return_5d_vs_nifty_pct=4.0,
        sector_pctile_effective_intent=80.0,
        adx_14=28.0,
        ema200_stretch_atr=3.0,
        cmf_20=0.1,
    )
    assert v2._sector_leader_carveout(audit)
    assert not v2._post_rally_chase_block(audit)


def test_compute_prior_session_streaks():
    rows = [
        {"action_signal": "accumulate", "tape_extras": {"forward_outcomes": {"forward_5d_pct": -2.0}}},
        {"action_signal": "buy", "tape_extras": {"forward_outcomes": {"forward_5d_pct": -1.0}}},
        {"action_signal": "hold", "tape_extras": {}},
        {"action_signal": "accumulate", "tape_extras": {}},
    ]
    streaks = v2.compute_prior_session_streaks(rows, window=5)
    assert streaks["prior_constructive_streak"] == 3
    assert streaks["prior_fail_streak"] == 2


def test_accumulate_band_raises_nw_when_risk_and_neg_cmf():
    audit = _base_audit(next_week_score=60.0, effective_intent_score=60.0, cmf_20=-0.02)
    assert v2._accumulate_band(audit, 2.5, buy_allowed=True) is False
    assert v2._accumulate_band(audit, 1.5, buy_allowed=True) is True


def test_hot_stretch_without_pullback_caps_constructive():
    """MOTHERSON-class: hot stretch without pullback cannot reach accumulate."""
    audit = _base_audit(
        ema200_stretch_atr=4.22,
        adx_14=63.5,
        return_5d_pct=6.5,
        cmf_20=0.05,
        next_week_score=65.0,
        effective_intent_score=68.0,
    )
    c = v2.layer_c(audit)
    assert v2._stretch_is_hot(audit, c)
    assert not v2._pullback_quality_proxy(audit)
    gate = v2._buy_gate(audit, {"buy_allowed": True}, c)
    assert gate["constructive_core"] is False
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "trim")


def test_hot_stretch_with_pullback_allows_constructive():
    audit = _base_audit(
        ema200_stretch_atr=4.5,
        return_1d_pct=-1.0,
        return_5d_pct=4.0,
        cmf_20=0.08,
        volume_participation_ratio=0.8,
        pullback_quality_proxy=True,
    )
    c = v2.layer_c(audit)
    assert v2._stretch_constructive_ok(audit, c)


def test_thin_liquidity_caps_at_accumulate():
    audit = _base_audit(
        liquidity_thin_proxy=True,
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    a = v2.layer_a(audit)
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "accumulate")
    assert label != "buy"


def test_short_history_caps_buy():
    audit = _base_audit(
        history_lt_200_sessions=True,
        next_week_score=72.0,
        effective_intent_score=70.0,
    )
    a = v2.layer_a(audit)
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"


def test_next_week_dampened_after_rally():
    """Stale high next_week after rally should fail buy score gate."""
    audit = _base_audit(
        next_week_score=70.8,
        return_5d_pct=15.2,
        effective_intent_score=69.0,
        rel_return_5d_vs_nifty_pct=5.0,
        prev_rel_return_5d_vs_nifty_pct=6.0,
    )
    eff_nw = v2._effective_next_week_for_gate(audit)
    assert eff_nw < 65.0
    gate = v2._buy_gate(audit, {"buy_allowed": True}, {"over_extension_hot": False})
    assert gate["constructive_scores"] is False


def test_very_negative_cmf_blocks_accumulate_core():
    """HEROMOTOCO-class CMF blocks constructive_core even without divergence."""
    audit = _base_audit(
        cmf_20=-0.21,
        return_1d_pct=-0.2,
        obv_trend_confirm=False,
        next_week_score=62.0,
        effective_intent_score=66.0,
    )
    gate = v2._buy_gate(audit, {"buy_allowed": True}, {"over_extension_hot": False})
    assert gate["constructive_core"] is False
