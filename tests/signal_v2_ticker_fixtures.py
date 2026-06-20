"""Minimal audit dicts for cited-ticker replay (Task 8 test matrix).

Metric values come from the Fixes.pdf / design-discussion evidence (+5.37% SYRMA,
+71% BALAMINES EMA stretch, etc.). Composite scores (next_week / intent) are set so
the legacy engine reproduces the cited *current* label; v2 expectations reflect the
implemented Layer C–E rules (documented per ticker in test docstrings).
"""

from __future__ import annotations

from typing import Any, TypedDict


class TickerCase(TypedDict):
    ticker: str
    audit: dict[str, Any]
    legacy_label: str
    v2_label: str
    driving_layer: str


_HOLLOW_BUY_BASE: dict[str, Any] = {
    "next_week_score": 72.0,
    "effective_intent_score": 66.0,
    "z_score": 2.4,
    "return_1d_pct": 5.37,
    "return_5d_pct": 3.0,
    "return_10d_pct": 3.0,
    "rel_return_5d_vs_nifty_pct": 2.0,
    "cmf_20": -0.113,
    "obv_slope_20": -5.0,
    "ema_200_distance_pct": 8.0,
    "ema200_stretch_atr": 2.67,
    "atr_14_pct": 3.0,
    "adx_14": 22.0,
}


TICKER_CASES: tuple[TickerCase, ...] = (
    {
        "ticker": "SYRMA",
        "audit": dict(_HOLLOW_BUY_BASE),
        "legacy_label": "buy",
        "v2_label": "accumulate",
        "driving_layer": "C-7 money-flow bear + D-2 hollow-breakout divergence",
    },
    {
        "ticker": "ALKYLAMINE",
        "audit": {
            **_HOLLOW_BUY_BASE,
            "return_1d_pct": 10.91,
            "z_score": 2.0,
            "cmf_20": -0.004,
        },
        "legacy_label": "buy",
        "v2_label": "buy",
        "driving_layer": "cmf in ±0.05 dead-band (no flow penalty)",
    },
    {
        "ticker": "MPHASIS",
        "audit": {**_HOLLOW_BUY_BASE, "return_1d_pct": 3.34, "cmf_20": -0.092},
        "legacy_label": "buy",
        "v2_label": "accumulate",
        "driving_layer": "C-7 mild distribution + D-2 divergence cap",
    },
    {
        "ticker": "INFY",
        "audit": {**_HOLLOW_BUY_BASE, "return_1d_pct": 5.68, "cmf_20": -0.103},
        "legacy_label": "buy",
        "v2_label": "accumulate",
        "driving_layer": "C-7 distribution + D-2 divergence on surge",
    },
    {
        "ticker": "CHENNPETRO",
        "audit": {
            **_HOLLOW_BUY_BASE,
            "return_1d_pct": 3.0,
            "ema_200_distance_pct": 14.30,
            "z_score": 2.55,
            "cmf_20": -0.08,
            "ema200_stretch_atr": 5.0,
        },
        "legacy_label": "buy",
        "v2_label": "accumulate",
        "driving_layer": "B Tier-2: C-8 over-extension hot + cmf distribution (recovery de-escalates trim)",
    },
    {
        "ticker": "BALAMINES",
        "audit": {
            **_HOLLOW_BUY_BASE,
            "return_1d_pct": 2.0,
            "ema_200_distance_pct": 71.0,
            "cmf_20": 0.15,
            "obv_slope_20": 5.0,
            "ema200_stretch_atr": 12.0,
            "z_score": 2.5,
        },
        "legacy_label": "buy",
        "v2_label": "accumulate",
        "driving_layer": "C-8 over-extension hot blocks buy; positive cmf → accumulate",
    },
    {
        "ticker": "JAGRAN",
        "audit": {
            "next_week_score": 58.0,
            "effective_intent_score": 53.0,
            "z_score": 1.5,
            "return_1d_pct": 0.5,
            "return_5d_pct": 2.0,
            "return_10d_pct": 2.0,
            "ema_200_distance_pct": 18.87,
            "cmf_20": -0.346,
            "ema200_stretch_atr": 6.0,
            "atr_14_pct": 3.0,
            "adx_14": 18.0,
        },
        "legacy_label": "hold",
        "v2_label": "trim",
        "driving_layer": "C-7 strong bear (capped) + C-8 stretch → risk_net trim band",
    },
    {
        "ticker": "HYUNDAI",
        "audit": {
            "next_week_score": 55.0,
            "effective_intent_score": 52.0,
            "z_score": 0.47,
            "return_1d_pct": -0.56,
            "return_5d_pct": -1.0,
            "return_10d_pct": -2.0,
            "cmf_20": -0.134,
            "ema_200_distance_pct": 5.0,
            "ema200_stretch_atr": 4.5,
            "atr_14_pct": 3.0,
            "adx_14": 19.0,
        },
        "legacy_label": "hold",
        "v2_label": "trim",
        "driving_layer": "B Tier-2: cmf distribution + over-extension hot (quiet markdown)",
    },
    {
        "ticker": "GREAVESCOT",
        "audit": {
            "next_week_score": 60.0,
            "effective_intent_score": 60.0,
            "z_score": 0.2,
            "return_1d_pct": 0.1,
            "return_5d_pct": 1.0,
            "return_10d_pct": 1.0,
            "cmf_20": 0.015,
            "ema_200_distance_pct": 24.54,
            "ema200_stretch_atr": 8.17,
            "atr_14_pct": 3.0,
            "adx_14": 19.88,
            "obv_slope_20": -2.0,
        },
        "legacy_label": "hold",
        "v2_label": "trim",
        "driving_layer": "D-4 stale-flow OBV tiebreaker → B Tier-2 trim (spec hold; impl trim)",
    },
    {
        "ticker": "ENDURANCE",
        "audit": {
            "next_week_score": 44.0,
            "effective_intent_score": 48.0,
            "z_score": 0.7,
            "return_1d_pct": -1.64,
            "return_5d_pct": 2.0,
            "return_10d_pct": 0.0,
            "volume_participation_ratio": 0.8,
            "cmf_20": 0.192,
            "ema_200_distance_pct": 5.0,
            "ema200_stretch_atr": 1.6,
            "atr_14_pct": 3.0,
            "adx_14": 22.0,
        },
        "legacy_label": "trim",
        "v2_label": "hold",
        "driving_layer": "D-3 healthy-pullback rescue (legacy trim from cooled intent)",
    },
)

TICKER_IDS = [c["ticker"] for c in TICKER_CASES]
