"""Cited-ticker replay matrix for signal v2 (Task 8).

Each case uses evidence metrics from the momentum-trap design discussion and asserts
legacy vs v2 labels under the appropriate env flags.
"""

from __future__ import annotations

import copy

import pytest

from action_signals import _derive_action_signal_legacy, derive_action_signal
from signal_v2_ticker_fixtures import TICKER_CASES


@pytest.fixture
def v2_accumulate_on(monkeypatch):
    monkeypatch.setenv("TITAN_SIGNAL_V2", "1")
    monkeypatch.setenv("TITAN_SIGV2_ENABLE_ACCUMULATE", "1")


@pytest.fixture
def v2_flags_off(monkeypatch):
    monkeypatch.delenv("TITAN_SIGNAL_V2", raising=False)
    monkeypatch.delenv("TITAN_SIGV2_ENABLE_ACCUMULATE", raising=False)


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c["ticker"]) for c in TICKER_CASES],
)
def test_cited_ticker_legacy_label(case):
    """Legacy path ignores flow/ADX; composites tuned to cited current label."""
    audit = copy.deepcopy(case["audit"])
    label, _risk, _ = _derive_action_signal_legacy(audit)
    assert label == case["legacy_label"], (
        f"{case['ticker']}: expected legacy {case['legacy_label']!r}, got {label!r}"
    )


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c["ticker"]) for c in TICKER_CASES],
)
def test_cited_ticker_v2_label(case, v2_accumulate_on):
    """V2 with accumulate enabled; see fixture driving_layer for rule attribution."""
    audit = copy.deepcopy(case["audit"])
    label, _risk, _ = derive_action_signal(audit)
    assert audit.get("signal_engine_version") == "v2"
    assert "signal_confidence" in audit
    assert label == case["v2_label"], (
        f"{case['ticker']}: expected v2 {case['v2_label']!r}, got {label!r} "
        f"(layer hint: {case['driving_layer']})"
    )


@pytest.mark.parametrize("case", [pytest.param(c, id=c["ticker"]) for c in TICKER_CASES])
def test_cited_ticker_flags_off_matches_legacy(case, v2_flags_off):
    """Golden: v2 master flag off stays byte-identical to legacy for cited audits."""
    audit = copy.deepcopy(case["audit"])
    got = derive_action_signal(audit)
    expected = _derive_action_signal_legacy(copy.deepcopy(case["audit"]))
    assert got == expected


def test_short_history_ceiling_blocks_buy_on_syrma(v2_accumulate_on):
    """Layer A: short history caps constructive labels even on strong composites."""
    from signal_v2_ticker_fixtures import TICKER_CASES

    audit = copy.deepcopy(next(c["audit"] for c in TICKER_CASES if c["ticker"] == "SYRMA"))
    audit["history_lt_200_sessions"] = True
    label, _risk, _ = derive_action_signal(audit)
    assert label == "hold"
    assert audit.get("signal_confidence", 1.0) < 1.0


def test_nan_heavy_withholds_constructive(v2_accumulate_on):
    """Layer A: NaN census >= 3 withholds buy/accumulate on otherwise-buy tape."""
    audit = {
        "next_week_score": 80.0,
        "effective_intent_score": 70.0,
        "history_lt_200_sessions": False,
    }
    label, _risk, _ = derive_action_signal(audit)
    assert label in ("hold", "trim", "exit-risk")
    assert label not in ("buy", "accumulate")


def test_thin_liquidity_forbids_buy_via_derive(v2_accumulate_on):
    """Layer A: thin liquidity forbids constructive labels."""
    audit = {
        "next_week_score": 80.0,
        "effective_intent_score": 70.0,
        "z_score": 1.5,
        "return_1d_pct": 2.0,
        "return_5d_pct": 2.0,
        "rel_return_5d_vs_nifty_pct": 1.0,
        "cmf_20": 0.10,
        "ema_200_distance_pct": 3.0,
        "ema200_stretch_atr": 1.5,
        "atr_14_pct": 2.0,
        "adx_14": 30.0,
        "liquidity_thin_proxy": True,
    }
    label, _risk, _ = derive_action_signal(audit)
    assert label != "buy"


def test_golden_representative_audits_flags_off(v2_flags_off):
    """Flags-off parity on the generic audits from test_signal_v2 (regression guard)."""
    from test_signal_v2 import _representative_audits

    for audit in _representative_audits():
        got = derive_action_signal(dict(audit))
        expected = _derive_action_signal_legacy(dict(audit))
        assert got == expected
