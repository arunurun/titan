"""Phase 2: market regime engine."""

from __future__ import annotations

import pytest


def test_strong_bull_classification():
    from market_regime import detect_market_regime

    out = detect_market_regime(nifty_above_ema200=True, breadth_pct=65.0, vix=15.0, nifty_adx=28.0)
    assert out["regime"] == "STRONG_BULL"


def test_bear_classification():
    from market_regime import detect_market_regime

    out = detect_market_regime(nifty_above_ema200=False, breadth_pct=25.0, vix=22.0, nifty_adx=18.0)
    assert out["regime"] == "BEAR"


def test_defensive_on_low_breadth():
    from market_regime import detect_market_regime

    out = detect_market_regime(nifty_above_ema200=True, breadth_pct=35.0, vix=20.0, nifty_adx=15.0)
    assert out["regime"] == "DEFENSIVE"


def test_buy_threshold_relax_in_strong_bull_enforce():
    from market_regime import apply_regime_to_audit, regime_adaptations
    from signal_v2 import _buy_gate, layer_a, layer_c

    adapts = regime_adaptations("STRONG_BULL")
    assert adapts["buy_threshold_delta"] == -5.0

    audit = {
        "nifty_above_ema200": True,
        "market_breadth_pct": 65.0,
        "india_vix": 15.0,
        "next_week_score": 62.0,
        "effective_intent_score": 57.0,
        "cmf_20": 0.1,
        "ema200_stretch_atr": 1.0,
    }
    apply_regime_to_audit(audit)
    assert audit["regime_buy_threshold_delta"] == -5.0
    a = layer_a(audit)
    c = layer_c(audit)
    gate = _buy_gate(audit, a, c)
    assert gate["constructive_scores"] is True


def test_bear_raises_defensive_multiplier():
    from market_regime import regime_adaptations

    adapts = regime_adaptations("BEAR")
    assert adapts["defensive_penalty_mult"] == pytest.approx(1.2)
    assert adapts["buy_threshold_delta"] == 5.0


def test_regime_always_applied(monkeypatch):
    from market_regime import apply_regime_to_audit

    monkeypatch.delenv("TITAN_REGIME_ENGINE_MODE", raising=False)
    audit: dict = {
        "nifty_above_ema200": True,
        "market_breadth_pct": 65.0,
        "india_vix": 15.0,
        "next_week_score": 70.0,
    }
    apply_regime_to_audit(audit)
    assert audit["market_regime"]["enabled"] is True
    assert audit["market_regime"]["mode"] == "enforce"
    assert audit["regime_buy_threshold_delta"] == -5.0


def test_shadow_mode_records_without_applying(monkeypatch):
    from market_regime import apply_regime_to_audit

    monkeypatch.setenv("TITAN_REGIME_ENGINE_MODE", "shadow")
    audit = {
        "nifty_above_ema200": True,
        "market_breadth_pct": 65.0,
        "india_vix": 15.0,
    }
    apply_regime_to_audit(audit)
    assert audit["market_regime"]["mode"] == "shadow"
    assert audit["market_regime"]["applied"] is False
    assert audit["regime_buy_threshold_delta"] == 0.0
