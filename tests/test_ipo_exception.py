"""IPO / short-history leader buy exception (flag-gated rollout)."""

from __future__ import annotations

import pytest

from action_signals import derive_action_signal
import signal_v2 as v2


@pytest.fixture(autouse=True)
def _enforce_ipo(monkeypatch):
    monkeypatch.setenv("TITAN_ENABLE_IPO_LEADER_EXCEPTION", "1")
    monkeypatch.setenv("TITAN_IPO_LEADER_EXCEPTION_MODE", "enforce")


def _ipo_leader_audit() -> dict:
    return {
        "history_lt_200_sessions": True,
        "next_week_score": 72.0,
        "effective_intent_score": 78.0,
        "z_score": 1.2,
        "return_1d_pct": 1.5,
        "return_5d_pct": 3.0,
        "return_21d_pct": 5.0,
        "return_63d_pct": 8.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.12,
        "obv_slope_20": 5.0,
        "obv_latest": 120.0,
        "obv_ema_20": 100.0,
        "obv_trend_confirm": True,
        "ema_200_distance_pct": 4.0,
        "ema200_stretch_atr": 1.5,
        "atr_14_pct": 2.0,
        "adx_14": 28.0,
        "adx_plus_di_14": 30.0,
        "adx_minus_di_14": 12.0,
        "volume_participation_ratio": 2.2,
        "fundamental_status": "strong",
    }


def test_ipo_leader_may_buy_when_risk_low():
    audit = _ipo_leader_audit()
    label, risk, _ = derive_action_signal(audit)
    assert risk < 2.0
    assert label == "buy"
    assert audit.get("ipo_leader_exception", {}).get("applied_buy") is True


def test_weak_ipo_stays_capped():
    audit = _ipo_leader_audit()
    audit["effective_intent_score"] = 55.0
    audit["next_week_score"] = 55.0
    a = v2.layer_a(audit)
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"


def test_thin_liquidity_blocks_ipo_buy():
    audit = _ipo_leader_audit()
    audit["liquidity_thin_proxy"] = True
    label, _, _ = derive_action_signal(audit)
    assert label != "buy"


def test_legacy_mode_accumulate_ceiling_only(monkeypatch):
    monkeypatch.delenv("TITAN_ENABLE_IPO_LEADER_EXCEPTION", raising=False)
    audit = _ipo_leader_audit()
    a = v2.layer_a(audit)
    assert a["buy_allowed"] is False
    assert a["label_ceiling"] == "accumulate"
