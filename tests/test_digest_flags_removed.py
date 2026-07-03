"""Digest always includes fusion pillar block and investment reports (no env flags)."""

from __future__ import annotations

import copy

import pytest


def test_digest_factor_scores_always_enabled(monkeypatch):
    from sector_audit import _digest_show_factor_scores_enabled

    monkeypatch.delenv("TITAN_DIGEST_SHOW_FACTOR_SCORES", raising=False)
    assert _digest_show_factor_scores_enabled() is True


def test_digest_investment_report_always_enabled(monkeypatch):
    from sector_audit import _digest_investment_report_enabled

    monkeypatch.delenv("TITAN_INVESTMENT_REPORT", raising=False)
    assert _digest_investment_report_enabled() is True


def test_simple_digest_includes_fusion_block():
    from sector_audit import _format_symbol_metrics_line_simple

    result = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "audit": {
            "effective_intent_score": 60.0,
            "z_score": 0.5,
            "return_1d_pct": 0.5,
            "atr_14_pct": 2.0,
            "adx_14": 24.0,
            "next_week_score": 62.0,
            "next_day_score": 58.0,
            "sell_signal": "hold",
            "sell_signal_risk_score": 2.5,
            "fundamental_status": "neutral",
            "hypothesis_support": "technical_only",
            "titan_fusion": {
                "titan_score": 58.0,
                "overall_confidence": 0.7,
                "contributions": {
                    "technical": {
                        "score": 60.0,
                        "weight_effective": 0.3,
                        "weighted": 18.0,
                    }
                },
            },
        },
    }
    text = _format_symbol_metrics_line_simple(copy.deepcopy(result))
    assert "Titan fusion" in text
    assert "REDUCE" in text or "HOLD" in text or "BUY" in text or "EXIT" in text
