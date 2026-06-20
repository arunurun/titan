"""action_signals: BUY / HOLD / TRIM / EXIT derivation."""

import math

from action_signals import (
    action_signal_from_digest_headline,
    action_signal_plain_english,
    action_style,
    derive_action_signal,
    normalize_action_signal,
)


def test_derive_buy_when_constructive_and_low_risk():
    sig, risk, reasons = derive_action_signal(
        {
            "next_week_score": 80.0,
            "effective_intent_score": 70.0,
            "z_score": 1.5,
            "return_1d_pct": 2.0,
            "return_5d_pct": 2.0,
            "return_10d_pct": 3.0,
            "rel_return_5d_vs_nifty_pct": 1.0,
            "cmf_20": 0.10,
            "obv_slope_20": 10.0,
            "ema_200_distance_pct": 3.0,
            "ema200_stretch_atr": 1.5,
            "atr_14_pct": 2.0,
            "adx_14": 30.0,
            "fundamental_status": "strong",
        }
    )
    assert sig == "buy"
    assert risk < 4.0
    assert reasons


def test_derive_hold_not_buy_when_trap_flag():
    sig, _, _ = derive_action_signal(
        {
            "next_week_score": 75.0,
            "effective_intent_score": 70.0,
            "trap_exit_proxy": True,
        }
    )
    assert sig != "buy"


def test_derive_hold_not_buy_when_liquidity_thin():
    sig, _, _ = derive_action_signal(
        {
            "next_week_score": 75.0,
            "effective_intent_score": 70.0,
            "return_1d_pct": 1.0,
            "liquidity_thin_proxy": True,
        }
    )
    assert sig != "buy"


def test_derive_hold_not_buy_when_extreme_move_proxy():
    sig, _, _ = derive_action_signal(
        {
            "next_week_score": 75.0,
            "effective_intent_score": 70.0,
            "return_1d_pct": 1.0,
            "extreme_price_move_proxy": True,
        }
    )
    assert sig != "buy"


def test_derive_trim_and_exit_bands():
    trim_sig, trim_risk, _ = derive_action_signal(
        {
            "next_week_score": 48.0,
            "effective_intent_score": 48.0,
            "return_1d_pct": -1.5,
            "return_5d_pct": -8.0,
            "return_21d_pct": -10.0,
            "return_63d_pct": -15.0,
            "return_126d_pct": -20.0,
            "event_risk_soon": True,
            "fundamental_status": "balanced",
        }
    )
    assert trim_sig == "trim"
    assert 5.0 <= trim_risk < 7.0

    exit_sig, exit_risk, _ = derive_action_signal(
        {
            "next_week_score": 40.0,
            "z_score": -2.4,
            "return_1d_pct": -2.6,
            "trap_exit_proxy": True,
            "fundamental_status": "weak",
        }
    )
    assert exit_sig == "exit-risk"
    assert exit_risk >= 7.0


def test_action_plain_english_includes_buy():
    assert "BUY" in action_signal_plain_english("buy")


def test_headline_parser_and_styles():
    assert action_signal_from_digest_headline("RELIANCE (NSE) — BUY — constructive") == "buy"
    assert action_signal_from_digest_headline("X (NSE) — TRIM — lighten") == "trim"
    assert action_signal_from_digest_headline("X (NSE) — EXIT RISK — cut") == "exit-risk"
    assert action_style("buy")["border"] == "#34a853"
    assert action_style("hold")["border"] == "#fbbc05"
    assert normalize_action_signal("exit_risk") == "exit-risk"
