"""Tests for Category-2 missed-rally fixes (momentum path, nw proxy, data hygiene)."""

from __future__ import annotations

import math

import signal_v2 as v2

from temp._backtest_60s_four_way import assess_data_quality, categorize


def _ideaforge_like_audit(**overrides):
    audit = {
        "effective_intent_score": 45.0,
        "intent_score": 45.0,
        "next_week_score": float("nan"),
        "cmf_20": 0.29,
        "adx_14": 32.0,
        "adx_plus_di_14": 35.0,
        "adx_minus_di_14": 10.0,
        "return_5d_pct": 12.0,
        "return_1d_pct": 2.0,
        "return_21d_pct": 15.0,
        "return_63d_pct": 20.0,
        "atr_14_pct": 3.5,
        "rel_return_5d_vs_nifty_pct": 8.0,
        "prev_rel_return_5d_vs_nifty_pct": 4.0,
        "ema200_stretch_atr": 5.0,
        "ema_200_distance_pct": 12.0,
        "sector_pctile_effective_intent": 80.0,
        "sector_pctile_return_5d_pct": 72.0,
        "indicator_trajectory": {
            "window": 5,
            "cmf_slope": 0.02,
            "cmf_deteriorating": False,
            "cmf_positive_sessions": 4,
            "adx_slope": 0.5,
            "adx_strong_bull_sessions": 3,
            "flow_tape_score": 0.2,
            "z_elevated_sessions": 1,
            "z_reverting": False,
        },
        "z_score": 1.5,
        "volume_participation_ratio": 1.3,
        "obv_trend_confirm": True,
    }
    audit.update(overrides)
    return audit


def test_momentum_continuation_ok_ideaforge_like():
    audit = _ideaforge_like_audit()
    assert v2._momentum_continuation_ok(audit)
    assert v2._momentum_stretch_relaxed(audit, {"over_extension_hot": False})


def test_ideaforge_like_maps_to_accumulate():
    audit = _ideaforge_like_audit(effective_intent_score=58.0, intent_score=58.0)
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "accumulate"


def test_sector_winner_lowers_intent_floor():
    audit = _ideaforge_like_audit(
        effective_intent_score=53.0,
        intent_score=53.0,
        next_week_score=60.0,
        sector_pctile_effective_intent=85.0,
        adx_14=28.0,
    )
    assert v2._accumulate_band(audit, 2.0, buy_allowed=True) is True


def test_nw_proxy_when_missing_and_cmf_improving():
    audit = _ideaforge_like_audit(
        next_week_score=float("nan"),
        effective_intent_score=56.0,
        cmf_20=0.18,
    )
    proxy = v2._effective_next_week_for_accumulate(audit)
    assert not math.isnan(proxy)
    assert proxy == 56.0


def test_stretch_4_6_positive_slopes_accumulate_ceiling_only():
    audit = _ideaforge_like_audit(
        ema200_stretch_atr=5.2,
        z_score=0.5,
        ema_200_distance_pct=10.0,
    )
    oe = v2._overextension_ceiling(audit)
    assert oe["ceiling"] == "accumulate"


def test_stretch_7_or_bearish_trajectory_hold_ceiling():
    audit = _ideaforge_like_audit(
        ema200_stretch_atr=7.5,
        indicator_trajectory={
            "window": 5,
            "cmf_slope": 0.02,
            "cmf_deteriorating": False,
            "cmf_positive_sessions": 4,
            "adx_slope": 0.5,
        },
    )
    assert v2._overextension_ceiling(audit)["ceiling"] == "hold"

    bearish = _ideaforge_like_audit(
        ema200_stretch_atr=3.0,
        indicator_trajectory={
            "window": 5,
            "cmf_slope": -0.05,
            "cmf_deteriorating": True,
            "cmf_positive_sessions": 0,
            "adx_slope": -0.1,
        },
    )
    assert v2._overextension_ceiling(bearish)["ceiling"] in ("hold", "accumulate")


def test_e2e_extreme_fwd5_excluded_from_scoring():
    audit = {"liquidity_thin_proxy": True, "extreme_price_move_proxy": True}
    dq = assess_data_quality(audit, fwd5=-91.4, fwd1=-5.0)
    assert dq["data_quality_excluded"] is True
    assert categorize("hold", "hold", False, False, data_quality_excluded=True) is None


def test_data_artifact_fwd5_excluded():
    dq = assess_data_quality({}, fwd5=54.0, fwd1=2.0)
    assert dq["data_quality_excluded"] is True
    assert "data_artifact_fwd5" in dq["data_quality_reasons"]
