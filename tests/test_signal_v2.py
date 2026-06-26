"""Unit tests for the v2 signal engine (signal_v2.py) and its dispatch.

Covers: v2-as-default smoke, per-layer behavior (A data-quality, C ramps + money-flow
+ over-extension, D modifiers, B two-tier, E mapping + buy-gate + accumulate + hysteresis).
"""

from __future__ import annotations

import pytest

from action_signals import _derive_action_signal_legacy, derive_action_signal
import signal_v2 as v2


# --------------------------------------------------------------------------- #
# representative audits
# --------------------------------------------------------------------------- #


def _clean_buy_audit() -> dict:
    return {
        "next_week_score": 80.0,
        "effective_intent_score": 70.0,
        "z_score": 1.5,
        "return_1d_pct": 2.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "return_21d_pct": 4.0,
        "return_63d_pct": 6.0,
        "return_126d_pct": 8.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.10,
        "obv_slope_20": 10.0,
        "ema_200_distance_pct": 3.0,
        "ema200_stretch_atr": 1.5,
        "atr_14_pct": 2.0,
        "adx_14": 30.0,
        "fundamental_status": "strong",
    }


def _representative_audits() -> list[dict]:
    return [
        _clean_buy_audit(),
        {  # weak tape -> high risk
            "next_week_score": 40.0, "effective_intent_score": 42.0, "z_score": -2.5,
            "return_1d_pct": -3.0, "return_5d_pct": -7.0, "return_10d_pct": -11.0,
            "ema_200_distance_pct": -8.0, "atr_14_pct": 7.0, "cmf_20": -0.3,
            "fundamental_status": "weak",
        },
        {  # middling -> hold/trim region
            "next_week_score": 58.0, "effective_intent_score": 53.0, "z_score": -0.5,
            "return_1d_pct": -0.5, "return_5d_pct": -2.5, "ema_200_distance_pct": -1.0,
            "atr_14_pct": 3.0, "cmf_20": 0.0,
        },
        {},  # empty / all-NaN
    ]


# --------------------------------------------------------------------------- #
# Default path always runs v2
# --------------------------------------------------------------------------- #


def test_derive_action_signal_always_runs_v2():
    audit = _clean_buy_audit()
    label, risk, _ = derive_action_signal(audit)
    assert audit.get("signal_engine_version") == "v2"
    assert "signal_confidence" in audit
    assert label == "buy"
    assert 0.0 <= risk <= 10.0


def test_v2_default_differs_from_legacy_on_representative_audits():
    """Smoke: production path is v2, not byte-identical to legacy."""
    diverged = 0
    for audit in _representative_audits():
        v2_out = derive_action_signal(dict(audit))
        leg_out = _derive_action_signal_legacy(dict(audit))
        if v2_out != leg_out:
            diverged += 1
    assert diverged >= 1


# --------------------------------------------------------------------------- #
# Layer A — data-quality / sanity
# --------------------------------------------------------------------------- #


def test_layer_a_short_history_caps_at_accumulate(monkeypatch):
    monkeypatch.delenv("TITAN_SIGNAL_V2_LAYER_A", raising=False)
    a = v2.layer_a({"history_lt_200_sessions": True, "z_score": 1.0, "cmf_20": 0.1})
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"
    assert a["confidence_seed"] < 1.0


def test_layer_a_nan_census_withholds_buy(monkeypatch):
    monkeypatch.delenv("TITAN_SIGNAL_V2_LAYER_A", raising=False)
    a = v2.layer_a({})  # all core metrics NaN
    assert a["buy_allowed"] is False
    assert a["confidence_seed"] < 1.0


def test_layer_a_thin_liquidity_forbids_buy():
    a = v2.layer_a({"liquidity_thin_proxy": True, "z_score": 1.0})
    assert a["buy_allowed"] is False


def test_layer_a_short_history_always_applies():
    a = v2.layer_a({"history_lt_200_sessions": True})
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"


def test_layer_a_boundary_blocks_buy_after_hysteresis():
    a = v2.layer_a({"liquidity_thin_proxy": True, "z_score": 1.0, "cmf_20": 0.1})
    assert v2._resolve_layer_a_final_label("buy", a) == "accumulate"


def test_layer_a_boundary_ceiling_caps_constructive():
    a = v2.layer_a({"history_lt_200_sessions": True, "z_score": 1.0, "cmf_20": 0.1})
    assert v2._resolve_layer_a_final_label("buy", a) == "accumulate"
    assert v2._resolve_layer_a_final_label("accumulate", a) == "accumulate"
    assert v2._resolve_layer_a_final_label("hold", a) == "hold"


# --------------------------------------------------------------------------- #
# Layer C — graded evidence
# --------------------------------------------------------------------------- #


def test_ramp_is_monotonic_and_clamped():
    assert v2._ramp(60.0, 55.0, 45.0, 3.0) == 0.0  # above zero edge
    assert v2._ramp(50.0, 55.0, 45.0, 3.0) == pytest.approx(1.5)
    assert v2._ramp(40.0, 55.0, 45.0, 3.0) == 3.0  # past full edge -> clamped


def test_ramp_nan_and_zero_denominator_safe():
    assert v2._ramp(float("nan"), 55.0, 45.0, 3.0) == 0.0
    assert v2._ramp(50.0, 45.0, 45.0, 3.0) == 0.0


def test_money_flow_deadband_and_scaling():
    # neutral CMF -> no term
    c = v2.layer_c({"cmf_20": 0.0})
    assert c["money_flow_bear"] == 0.0 and c["money_flow_bull"] == 0.0
    # distribution -> bear scaled by magnitude
    c = v2.layer_c({"cmf_20": -0.113})
    assert c["money_flow_bear"] == pytest.approx((0.063) * 10.0, abs=1e-6)
    # accumulation -> bull
    c = v2.layer_c({"cmf_20": 0.192})
    assert c["money_flow_bull"] == pytest.approx((0.142) * 10.0, abs=1e-6)


def test_obv_only_amplifies_existing_cmf_term():
    base = v2.layer_c({"cmf_20": -0.113})["money_flow_bear"]
    amp = v2.layer_c(
        {"cmf_20": -0.113, "obv_trend_confirm": False, "obv_latest": 100.0, "obv_ema_20": 120.0}
    )["money_flow_bear"]
    assert amp == pytest.approx(base * 1.25, abs=1e-6)
    bull_base = v2.layer_c({"cmf_20": 0.192})["money_flow_bull"]
    bull_amp = v2.layer_c(
        {"cmf_20": 0.192, "obv_trend_confirm": True, "obv_latest": 120.0, "obv_ema_20": 100.0}
    )["money_flow_bull"]
    assert bull_amp == pytest.approx(bull_base * 1.25, abs=1e-6)
    none = v2.layer_c({"cmf_20": 0.0, "obv_trend_confirm": False})
    assert none["money_flow_bear"] == 0.0


def test_over_extension_is_atr_normalized_not_flat_pct():
    # +20% above EMA200 but only ~2 ATRs of stretch -> NOT hot
    calm = v2.layer_c({"ema_200_distance_pct": 20.0, "ema200_stretch_atr": 2.0})
    assert calm["over_extension"] == 0.0 and calm["over_extension_hot"] is False
    # same distance but 8 ATRs -> fully hot at cap
    wild = v2.layer_c({"ema_200_distance_pct": 20.0, "ema200_stretch_atr": 8.0})
    assert wild["over_extension"] == pytest.approx(2.0)
    assert wild["over_extension_hot"] is True


# --------------------------------------------------------------------------- #
# Layer D — context modifiers
# --------------------------------------------------------------------------- #


def test_adx_regime_multipliers():
    weak = v2.layer_d({"adx_14": 15.0}, {})
    assert weak["mult_money_flow"] == 1.3 and weak["mult_momentum"] == 0.7
    bull = v2.layer_d(
        {"adx_14": 30.0, "adx_plus_di_14": 28.0, "adx_minus_di_14": 12.0}, {}
    )
    assert bull["mult_momentum"] == 1.3 and bull["mult_risk"] == 0.8
    bear = v2.layer_d(
        {"adx_14": 30.0, "adx_plus_di_14": 12.0, "adx_minus_di_14": 28.0}, {}
    )
    assert bear["mult_momentum"] == 0.5 and bear["mult_risk"] == 1.5
    deadband = v2.layer_d(
        {"adx_14": 22.0, "prev_adx_regime_mults": {"mult_momentum": 1.3, "mult_risk": 0.8}},
        {},
    )
    assert deadband["mult_momentum"] == 1.3 and deadband["mult_risk"] == 0.8


def test_adx_nan_preserves_prior_regime_multipliers():
    d = v2.layer_d(
        {
            "prev_adx_regime_mults": {
                "mult_money_flow": 1.3,
                "mult_over_extension": 1.3,
                "mult_momentum": 0.7,
                "mult_risk": 1.0,
            }
        },
        {},
    )
    assert d["mult_money_flow"] == 1.3
    assert d["mult_over_extension"] == 1.3
    assert d["mult_momentum"] == 0.7
    assert d["mult_risk"] == 1.0


def test_divergence_caps_buy_confidence():
    d = v2.layer_d(
        {
            "return_1d_pct": 5.0,
            "cmf_20": -0.1,
            "adx_14": 22.0,
            "obv_trend_confirm": False,
            "obv_latest": 90.0,
            "obv_ema_20": 100.0,
        },
        {},
    )
    assert d["divergence_bump"] == 1.0
    assert d["buy_confidence_cap"] == 0.5


def test_hollow_breakout_blocked_when_obv_trend_confirm():
    d = v2.layer_d(
        {
            "return_1d_pct": 5.0,
            "cmf_20": -0.1,
            "adx_14": 22.0,
            "obv_trend_confirm": True,
            "obv_latest": 120.0,
            "obv_ema_20": 100.0,
        },
        {},
    )
    assert d["divergence_bump"] == 0.0
    assert d["buy_confidence_cap"] is None


def test_institutional_absorption_bump_and_halves_layer_c_risk():
    audit = {
        "return_1d_pct": -1.5,
        "cmf_20": 0.12,
        "obv_trend_confirm": True,
        "obv_latest": 120.0,
        "obv_ema_20": 100.0,
        "adx_14": 22.0,
    }
    d = v2.layer_d(audit, {"over_extension_hot": False})
    assert d["pullback_bull_bump"] == pytest.approx(0.75)
    assert d["layer_c_risk_mult"] == pytest.approx(0.5)
    c = v2.layer_c(
        {
            "next_week_score": 40.0,
            "effective_intent_score": 42.0,
            "z_score": -2.0,
            "return_1d_pct": -1.5,
            "return_5d_pct": -3.0,
            "return_10d_pct": -5.0,
            "ema_200_distance_pct": -2.0,
            "atr_14_pct": 4.0,
            "cmf_20": 0.12,
        }
    )
    base_agg = v2._aggregate(c, {"mult_momentum": 1.0, "mult_money_flow": 1.0, "mult_over_extension": 1.0, "mult_risk": 1.0, "layer_c_risk_mult": 1.0, "divergence_bump": 0.0, "pullback_bull_bump": 0.0})
    abs_agg = v2._aggregate(c, d)
    assert abs_agg["risk_c"] == pytest.approx(base_agg["risk_c"] * 0.5, abs=0.05)
    assert abs_agg["bull_c"] > base_agg["bull_c"]


def test_healthy_pullback_rescue():
    d = v2.layer_d(
        {
            "return_1d_pct": -1.64, "volume_participation_ratio": 0.8, "cmf_20": 0.192,
            "return_5d_pct": 2.0, "ema_200_distance_pct": 5.0, "adx_14": 22.0,
        },
        {},
    )
    assert d["mult_momentum"] == 0.5
    assert d["pullback_bull_bump"] > 0.0


def test_healthy_pullback_halves_not_caps_momentum_mult():
    """Halving (×0.5) must not floor a strong-trend mult at 0.5."""
    d = v2.layer_d(
        {
            "return_1d_pct": -1.0,
            "volume_participation_ratio": 0.8,
            "cmf_20": 0.12,
            "return_5d_pct": 2.0,
            "ema_200_distance_pct": 5.0,
            "adx_14": 28.0,
            "adx_plus_di_14": 30.0,
            "adx_minus_di_14": 12.0,
        },
        {},
    )
    assert d["mult_momentum"] == pytest.approx(0.65, abs=0.01)
    assert d["pullback_bull_bump"] > 0.0


def test_staleflow_obv_tiebreaker():
    d = v2.layer_d(
        {
            "cmf_20": 0.015,
            "adx_14": 19.88,
            "obv_trend_confirm": False,
            "obv_latest": 90.0,
            "obv_ema_20": 100.0,
        },
        {"over_extension_hot": True},
    )
    assert d["staleflow_downgrade"] is True


# --------------------------------------------------------------------------- #
# Layer B — two-tier hard disqualifiers
# --------------------------------------------------------------------------- #


def test_layer_b_tier1_instant_exit_bypasses_hysteresis():
    b = v2.layer_b(
        {"structural_break_proxy": True, "return_1d_pct": -9.0}, {}, {}
    )
    assert b["forced_label"] == "exit-risk"
    assert b["bypass_hysteresis"] is True


def test_layer_b_vpr_proxies_count_as_one():
    # both VPR-derived proxies + cmf distribution => 2 distinct corroborators -> trim
    b = v2.layer_b(
        {
            "trap_exit_proxy": True, "high_volume_down_day_proxy": True,
            "cmf_20": -0.1,
        },
        {"over_extension_hot": False},
        {},
    )
    assert b["corroborators"] == 2
    assert b["forced_label"] == "trim"


def test_layer_b_options_into_call_wall_corroborator():
    b = v2.layer_b(
        {
            "option_chain_unavailable": False,
            "close_last": 104.9,
            "call_oi_wall_strike": 105.0,
            "put_oi_wall_strike": 95.0,
            "sell_signal": "trim",
            "cmf_20": -0.1,
            "return_1d_pct": -1.0,
        },
        {"over_extension_hot": False},
        {},
    )
    assert "into call OI wall" in " ".join(b.get("reasons", []))


def test_layer_b_options_below_put_support_corroborator():
    b = v2.layer_b(
        {
            "option_chain_unavailable": False,
            "close_last": 94.0,
            "call_oi_wall_strike": 110.0,
            "put_oi_wall_strike": 95.0,
            "cmf_20": -0.2,
            "return_1d_pct": -2.0,
        },
        {"over_extension_hot": False},
        {},
    )
    assert "below put OI support" in " ".join(b.get("reasons", []))


def test_layer_b_three_corroborators_exit():
    b = v2.layer_b(
        {
            "trap_exit_proxy": True, "cmf_20": -0.1, "event_risk_soon": True,
        },
        {"over_extension_hot": True},
        {},
    )
    assert b["corroborators"] >= 3
    assert b["forced_label"] == "exit-risk"


# --------------------------------------------------------------------------- #
# Layer E — mapping, buy gate, accumulate gating, hysteresis
# --------------------------------------------------------------------------- #


def test_hollow_breakout_demotes_buy_to_accumulate():
    audit = {
        "next_week_score": 72.0, "effective_intent_score": 66.0, "z_score": 2.4,
        "return_1d_pct": 5.37, "return_5d_pct": 3.0, "rel_return_5d_vs_nifty_pct": 2.0,
        "cmf_20": -0.113, "obv_slope_20": -5.0, "ema_200_distance_pct": 8.0,
        "ema200_stretch_atr": 2.67, "atr_14_pct": 3.0, "adx_14": 22.0,
    }
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "accumulate"


def test_greavescot_staleflow_downgrades_hold_to_trim():
    audit = {
        "next_week_score": 60.0, "effective_intent_score": 60.0, "z_score": 0.2,
        "return_1d_pct": 0.1, "return_5d_pct": 1.0, "cmf_20": 0.015,
        "ema_200_distance_pct": 24.54, "ema200_stretch_atr": 8.17, "atr_14_pct": 3.0,
        "adx_14": 19.88, "obv_trend_confirm": False, "obv_latest": 90.0, "obv_ema_20": 100.0,
    }
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "trim"


def test_endurance_healthy_pullback_not_trimmed():
    audit = {
        "next_week_score": 58.0, "effective_intent_score": 55.0, "z_score": 0.7,
        "return_1d_pct": -1.64, "return_5d_pct": 2.0, "volume_participation_ratio": 0.8,
        "cmf_20": 0.192, "ema_200_distance_pct": 5.0, "ema200_stretch_atr": 1.6,
        "atr_14_pct": 3.0, "adx_14": 22.0,
    }
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "accumulate", "buy")  # rescued, not a trim/exit


def test_hysteresis_buffer_holds_trim_label():
    # risk_net below trim floor but prior=trim => stickiness keeps trim
    label, applied = v2._apply_hysteresis(
        "hold", 4.2, prior_label="trim", bypass=False, buffer=0.5
    )
    assert label == "trim" and applied is True


def test_hysteresis_blocks_constructive_until_buy_ceiling():
    label, applied = v2._apply_hysteresis(
        "accumulate", 3.5, prior_label="trim", bypass=False, buffer=0.5, audit={}
    )
    assert label == "hold" and applied is True
    label2, applied2 = v2._apply_hysteresis(
        "accumulate",
        2.5,
        prior_label="trim",
        bypass=False,
        buffer=0.5,
        audit={"effective_intent_score": 70.0, "next_week_score": 65.0, "return_5d_pct": 2.0},
    )
    assert label2 == "accumulate" and applied2 is False


def test_hysteresis_danger_is_fast():
    label, applied = v2._apply_hysteresis(
        "exit-risk", 8.0, prior_label="hold", bypass=True, buffer=0.5
    )
    assert label == "exit-risk" and applied is False


# --------------------------------------------------------------------------- #
# Phase 2 — next-open gap entry guard (shadow-first)
# --------------------------------------------------------------------------- #


def _gap_buy_audit(**extra) -> dict:
    base = _clean_buy_audit()
    base.update(extra)
    return base


def test_gap_guard_shadow_logs_without_changing_label(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "shadow")
    audit = _gap_buy_audit(next_open_gap_pct=4.0)
    label, _, _ = v2.evaluate_signal_v2(audit)
    gg = audit["gap_guard"]
    assert label == "buy"
    assert gg["mode"] == "shadow"
    assert gg["would_action"] == "damp"
    assert gg["gap_pct"] == pytest.approx(4.0)
    assert gg["applied_ceiling"] is None
    assert gg["applied_forced_label"] is None


def test_gap_guard_damp_caps_gap_up_buy(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "damp")
    audit = _gap_buy_audit(next_open_gap_pct=3.0)
    label, _, _ = v2.evaluate_signal_v2(audit)
    gg = audit["gap_guard"]
    assert gg["would_action"] == "damp"
    assert label == "accumulate"
    assert gg["applied_ceiling"] == "accumulate"


def test_gap_guard_damp_extreme_gap_up_holds(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "damp")
    audit = _gap_buy_audit(next_open_gap_pct=6.0)
    label, _, _ = v2.evaluate_signal_v2(audit)
    assert audit["gap_guard"]["would_ceiling"] == "hold"
    assert label == "hold"


def test_gap_guard_skip_escalates_gap_down_buy(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "skip")
    audit = _gap_buy_audit(next_open_gap_pct=-2.0)
    label, _, _ = v2.evaluate_signal_v2(audit)
    gg = audit["gap_guard"]
    assert gg["would_action"] == "skip"
    assert label == "hold"
    assert gg["applied_forced_label"] == "hold"


def test_gap_guard_skip_severe_gap_down_trims(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "skip")
    audit = _gap_buy_audit(next_open_gap_pct=-4.0)
    label, _, _ = v2.evaluate_signal_v2(audit)
    gg = audit["gap_guard"]
    assert gg["would_forced_label"] == "trim"
    assert label == "trim"


def test_gap_guard_nan_is_noop(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "skip")
    audit = _gap_buy_audit()
    label, _, _ = v2.evaluate_signal_v2(audit)
    gg = audit["gap_guard"]
    assert label == "buy"
    assert gg["would_action"] is None
    assert gg["reason"] == "no_gap_data"


def test_gap_guard_derives_from_return_series(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "shadow")
    audit = _gap_buy_audit(
        return_series=[
            {"close": 100.0},
            {"open": 104.0},
        ]
    )
    gap_pct, source = v2._derive_next_open_gap_pct(audit)
    assert gap_pct == pytest.approx(4.0)
    assert source == "return_series"
    label, _, _ = v2.evaluate_signal_v2(audit)
    assert audit["gap_guard"]["would_action"] == "damp"


def test_datamatics_case_accumulates_on_loosened_gate():
    """Intent 73.75 / next_week 67.09 blocked buy at old 70/65 gates → accumulate."""
    audit = {
        "next_week_score": 67.09,
        "effective_intent_score": 73.75,
        "z_score": 1.2,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": -0.08,
        "ema_200_distance_pct": 5.0,
        "ema200_stretch_atr": 2.5,
        "atr_14_pct": 2.5,
        "adx_14": 25.0,
        "volume_participation_ratio": 1.35,
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < v2._buy_risk_ceiling()
    assert label == "accumulate"


def test_rally_recovery_caps_tier2_trim_to_accumulate():
    """SONATSOFTW-class: rally tape with corroborators should not stay on trim."""
    audit = {
        "next_week_score": 62.0,
        "effective_intent_score": 68.0,
        "z_score": 1.0,
        "return_1d_pct": 2.5,
        "return_5d_pct": 4.0,
        "return_10d_pct": 6.0,
        "rel_return_5d_vs_nifty_pct": 2.0,
        "cmf_20": -0.06,
        "ema_200_distance_pct": 12.0,
        "ema200_stretch_atr": 3.5,
        "atr_14_pct": 3.0,
        "adx_14": 18.0,
        "adx_plus_di_14": 15.0,
        "adx_minus_di_14": 22.0,
        "high_volume_down_day_proxy": True,
        "prev_action_signal": "trim",
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < v2._buy_risk_ceiling()
    assert label in ("hold", "accumulate")


def test_recovery_deescalates_prior_trim_when_tape_recovers():
    audit = {
        "next_week_score": 58.0,
        "effective_intent_score": 62.0,
        "z_score": 0.4,
        "return_1d_pct": 0.8,
        "return_5d_pct": 1.5,
        "cmf_20": 0.05,
        "ema_200_distance_pct": 6.0,
        "ema200_stretch_atr": 2.0,
        "atr_14_pct": 2.5,
        "adx_14": 24.0,
        "prev_action_signal": "trim",
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < v2._buy_risk_ceiling()
    assert label in ("hold", "accumulate")


def test_reescalate_trim_blocked_without_defensive_streak():
    label, applied = v2._apply_hysteresis(
        "trim",
        3.6,
        prior_label="hold",
        bypass=False,
        buffer=0.5,
        audit={
            "effective_intent_score": 62.0,
            "next_week_score": 58.0,
            "prev_action_signal": "hold",
        },
    )
    assert label == "hold" and applied is True


def test_short_history_strong_vpr_accumulates():
    audit = {
        "next_week_score": 72.0,
        "effective_intent_score": 70.0,
        "z_score": 1.5,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.10,
        "ema_200_distance_pct": 3.0,
        "ema200_stretch_atr": 1.5,
        "atr_14_pct": 2.0,
        "adx_14": 28.0,
        "volume_participation_ratio": 1.6,
        "history_lt_200_sessions": True,
    }
    label, _risk, _ = v2.evaluate_signal_v2(audit)
    assert label == "accumulate"


def test_tier2_e2e_intent_led_deescalates_trim_to_accumulate():
    """E2E Jun 15-class: Tier-2 trim + strong intent/VPR + rally day -> accumulate."""
    audit = {
        "next_week_score": 65.49,
        "effective_intent_score": 73.96,
        "z_score": 1.0,
        "return_1d_pct": 4.72,
        "return_5d_pct": -14.68,
        "return_10d_pct": -1.5,
        "rel_return_5d_vs_nifty_pct": 2.0,
        "cmf_20": -0.063,
        "obv_slope_20": 5.0,
        "ema_200_distance_pct": 15.0,
        "ema200_stretch_atr": 4.84,
        "atr_14_pct": 3.0,
        "adx_14": 37.7,
        "volume_participation_ratio": 3.88,
        "prev_action_signal": "trim",
    }
    label, risk, reasons = v2.evaluate_signal_v2(audit)
    assert "Tier-2" in " ".join(reasons)
    assert risk < 5.0
    assert label in ("hold", "accumulate", "trim")


def test_tier2_netweb_overext_cmf_rally_recovers_to_hold_or_accumulate():
    """NETWEB-class: overextension + CMF distribution but rally tape recovers."""
    audit = {
        "next_week_score": 58.0,
        "effective_intent_score": 66.0,
        "z_score": 0.8,
        "return_1d_pct": 2.0,
        "return_5d_pct": 3.5,
        "cmf_20": -0.07,
        "ema_200_distance_pct": 10.0,
        "ema200_stretch_atr": 3.83,
        "atr_14_pct": 2.8,
        "adx_14": 22.0,
        "volume_participation_ratio": 1.25,
        "prev_action_signal": "trim",
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < 5.0
    assert label in ("hold", "accumulate")


def test_leader_participation_floor():
    audit = {
        "next_week_score": 58.0,
        "effective_intent_score": 68.0,
        "z_score": 0.5,
        "return_1d_pct": 0.5,
        "return_5d_pct": 1.0,
        "cmf_20": 0.05,
        "ema_200_distance_pct": 4.0,
        "ema200_stretch_atr": 1.2,
        "atr_14_pct": 2.5,
        "adx_14": 22.0,
        "volume_participation_ratio": 1.8,
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert risk < v2._buy_risk_ceiling()
    assert label == "accumulate"


def test_buy_gate_env_overrides(monkeypatch):
    monkeypatch.setenv("TITAN_SIGV2_BUY_NEXT_WEEK_MIN", "75")
    monkeypatch.setenv("TITAN_SIGV2_BUY_INTENT_MIN", "70")
    audit = _clean_buy_audit()
    audit["next_week_score"] = 72.0
    audit["effective_intent_score"] = 68.0
    label, _, _ = v2.evaluate_signal_v2(audit)
    assert label in ("hold", "accumulate")
    assert label != "buy"


def test_gap_guard_session_move_proxy_when_flagged(monkeypatch):
    monkeypatch.setenv("TITAN_GAP_GUARD_MODE", "shadow")
    audit = _gap_buy_audit(
        gap_guard_next_open_proxy=True,
        session_move_vs_prev_close_pct=3.2,
    )
    gap_pct, source = v2._derive_next_open_gap_pct(audit)
    assert gap_pct == pytest.approx(3.2)
    assert source == "session_move_vs_prev_close_pct"


def test_constructive_allowed_thin_liquidity_strong_scores():
    a = v2.layer_a({"liquidity_thin_proxy": True, "z_score": 1.0, "cmf_20": 0.1})
    audit = {
        "liquidity_thin_proxy": True,
        "effective_intent_score": 68.0,
        "next_week_score": 62.0,
    }
    assert a["buy_allowed"] is False
    assert v2._constructive_allowed(a, audit) is True


def test_thin_liquidity_accumulate_not_buy():
    """Thin liquidity: strong scores may accumulate but never buy."""
    audit = {
        "liquidity_thin_proxy": True,
        "next_week_score": 66.0,
        "effective_intent_score": 68.0,
        "z_score": 1.2,
        "return_1d_pct": 1.0,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.08,
        "ema_200_distance_pct": 5.0,
        "ema200_stretch_atr": 1.8,
        "atr_14_pct": 2.5,
        "adx_14": 25.0,
        "volume_participation_ratio": 1.4,
    }
    label, risk, _ = v2.evaluate_signal_v2(audit)
    assert label in ("accumulate", "hold")
    assert label != "buy"
    assert risk < v2._buy_risk_ceiling()


def test_accumulate_band_uses_loosened_defaults():
    """Scores 58/58 should pass accumulate band when risk is strictly below buy ceiling."""
    assert v2._accumulate_band(
        {"next_week_score": 58.0, "effective_intent_score": 58.0},
        2.9,
        buy_allowed=True,
    )


def test_recovery_tape_ok_allows_risk_below_recovery_max():
    audit = {
        "effective_intent_score": 62.0,
        "next_week_score": 58.0,
        "return_5d_pct": 1.5,
    }
    assert v2._recovery_tape_ok(audit, 4.5, rally=False) is True
    assert v2._recovery_tape_ok(audit, 5.5, rally=False) is False


def test_bullish_adx_suppresses_volatility_family():
    base = v2._family_points({"atr_penalty_input": 2.0, "atr_14_pct": 5.0})
    suppressed = v2._family_points(
        {
            "atr_penalty_input": 2.0,
            "atr_14_pct": 5.0,
            "adx_14": 30.0,
            "adx_plus_di_14": 28.0,
            "adx_minus_di_14": 10.0,
        }
    )
    assert base["families"]["volatility"] > 0.0
    assert suppressed["families"]["volatility"] == 0.0


def test_bullish_adx_widens_stretch_deadband():
    narrow = v2.layer_c({"ema200_stretch_atr": 4.0, "adx_14": 15.0})
    wide = v2.layer_c(
        {
            "ema200_stretch_atr": 4.0,
            "adx_14": 30.0,
            "adx_plus_di_14": 28.0,
            "adx_minus_di_14": 10.0,
        }
    )
    assert narrow["over_extension"] > 0.0
    assert wide["over_extension"] == 0.0
    assert wide["over_extension_hot"] is False


def test_map_label_uses_asymmetric_risk_thresholds():
    gate = {"clean_buy": True, "constructive_core": True, "constructive_scores": True}
    a = {"buy_allowed": True}
    assert v2._map_label(2.5, gate, {}, a) == "buy"
    assert v2._map_label(3.5, gate, {}, a) == "hold"
    assert v2._map_label(5.5, gate, {}, a) == "trim"
    assert v2._map_label(6.9, gate, {}, a) == "trim"
    assert v2._map_label(7.0, gate, {}, a) == "exit-risk"
    assert v2._map_label(7.5, gate, {}, a) == "exit-risk"


def test_prior_trim_risk_35_forces_hold_not_accumulate():
    """Prior trim + risk_net in recovery deadband must not re-enter accumulate."""
    gate = {"clean_buy": False, "constructive_core": True, "constructive_scores": True}
    a = {"buy_allowed": True}
    audit = {"prev_action_signal": "trim", "next_week_score": 66.0, "effective_intent_score": 68.0}
    assert v2._map_label(3.5, gate, audit, a) == "hold"
    assert v2._apply_prior_defensive_deadband("accumulate", 3.5, "trim") == "hold"
    assert v2._apply_prior_defensive_deadband("accumulate", 2.5, "trim") == "accumulate"
