"""Unit tests for Titan efficacy signal fixes (Cat 2 / Cat 4 / false defensives)."""

from __future__ import annotations

import math

import signal_v2 as v2


def _leader_audit(**overrides):
    audit = {
        "effective_intent_score": 58.0,
        "intent_score": 58.0,
        "next_week_score": 62.0,
        "cmf_20": 0.12,
        "adx_14": 28.0,
        "adx_plus_di_14": 30.0,
        "adx_minus_di_14": 12.0,
        "return_5d_pct": 8.0,
        "sector_pctile_effective_intent": 80.0,
        "sector_pctile_cmf_20": 60.0,
        "ema200_stretch_atr": 4.0,
        "indicator_trajectory": {
            "window": 5,
            "cmf_slope": 0.02,
            "cmf_deteriorating": False,
            "cmf_deteriorating_sessions": 0,
            "cmf_positive_sessions": 4,
            "adx_slope": 0.3,
        },
    }
    audit.update(overrides)
    return audit


def test_sector_leader_carveout_v2_stretch_5_6_atr():
    audit = _leader_audit(
        ema200_stretch_atr=5.4,
        indicator_trajectory={
            "window": 5,
            "cmf_slope": 0.03,
            "cmf_deteriorating": False,
            "cmf_positive_sessions": 4,
            "adx_slope": 0.2,
        },
    )
    assert not v2._sector_leader_carveout(audit)
    assert v2._sector_leader_carveout(audit, risk_net=3.2)
    oe = v2._overextension_ceiling(audit, risk_net=3.2)
    assert oe["ceiling"] in (None, "accumulate")


def test_sector_relative_cmf_blocks_only_bottom_quartile():
    weak = _leader_audit(cmf_20=-0.02, sector_pctile_cmf_20=20.0)
    ok = _leader_audit(cmf_20=-0.02, sector_pctile_cmf_20=30.0)
    assert v2._cmf_sector_weak(weak)
    assert not v2._cmf_sector_weak(ok)
    assert v2._cmf_distribution_corroborator(weak)
    assert not v2._cmf_distribution_corroborator(ok)


def test_cmf_floor_only_when_deteriorating_2_sessions():
    def _accum_cmf_ok(audit: dict) -> bool:
        cmf = audit["cmf_20"]
        ok = not v2._cmf_sector_weak(audit)
        if v2._cmf_trajectory_deteriorating_2plus(audit):
            ok = ok and cmf >= -0.05
        return ok

    mild = _leader_audit(
        cmf_20=-0.08,
        sector_pctile_cmf_20=40.0,
        indicator_trajectory={
            "window": 5,
            "cmf_slope": -0.02,
            "cmf_deteriorating": True,
            "cmf_deteriorating_sessions": 2,
            "cmf_positive_sessions": 1,
        },
    )
    stable = _leader_audit(
        cmf_20=-0.03,
        sector_pctile_cmf_20=40.0,
        indicator_trajectory={
            "window": 5,
            "cmf_slope": 0.01,
            "cmf_deteriorating": False,
            "cmf_deteriorating_sessions": 0,
            "cmf_positive_sessions": 3,
        },
    )
    assert _accum_cmf_ok(stable)
    assert not _accum_cmf_ok(mild)


def test_intent_override_winners_requires_adx():
    audit = _leader_audit(
        effective_intent_score=53.0,
        intent_score=53.0,
        sector_pctile_effective_intent=85.0,
        adx_14=22.0,
    )
    assert v2._accumulate_intent_floor(audit, 58.0) == 58.0
    audit["adx_14"] = 26.0
    assert v2._accumulate_intent_floor(audit, 58.0) == 52.0


def test_nw_proxy_intent_55_cmf_015():
    audit = _leader_audit(
        next_week_score=float("nan"),
        effective_intent_score=56.0,
        cmf_20=0.18,
    )
    proxy = v2._effective_next_week_for_accumulate(audit)
    assert not math.isnan(proxy)
    assert proxy == 56.0

    low_cmf = _leader_audit(
        next_week_score=float("nan"),
        effective_intent_score=56.0,
        cmf_20=0.10,
    )
    assert math.isnan(v2._effective_next_week_for_accumulate(low_cmf))


def test_risk_soft_band_allows_accumulate():
    audit = _leader_audit(
        next_week_score=60.0,
        effective_intent_score=60.0,
        cmf_20=0.12,
    )
    assert v2._accumulate_risk_ok(audit, 3.2)
    assert v2._accumulate_band(audit, 3.2, buy_allowed=True)
    bearish = _leader_audit(
        indicator_trajectory={
            "window": 5,
            "cmf_slope": -0.05,
            "cmf_deteriorating": True,
            "cmf_deteriorating_sessions": 2,
            "cmf_positive_sessions": 0,
            "flow_tape_score": -0.2,
        },
    )
    assert not v2._accumulate_risk_ok(bearish, 3.2)


def test_momentum_sector_positive_tape_needs_two_corroborators_for_trim():
    audit = {
        "sector_key": "defence",
        "return_5d_pct": 3.0,
        "cmf_20": 0.08,
        "staleflow_downgrade": True,
    }
    c = {"over_extension_hot": False}
    d = {"staleflow_downgrade": True}
    b = v2.layer_b(audit, c, d)
    assert b["forced_label"] is None
    audit["trap_exit_proxy"] = True
    b2 = v2.layer_b(audit, c, d)
    assert b2["forced_label"] == "trim"
    assert b2["corroborators"] >= 2
