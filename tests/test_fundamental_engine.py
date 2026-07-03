"""Unit tests for fundamental_engine."""

from __future__ import annotations

import pytest


def test_score_fundamentals_strong():
    from fundamental_engine import score_fundamentals

    row = {"roe": 18.0, "roce": 16.0, "debt_to_equity": 0.3, "net_profit_margin": 14.0}
    out = score_fundamentals(row)
    assert out["available"] is True
    assert out["score"] >= 65.0
    assert out["metadata"]["status"] == "strong"


def test_score_fundamentals_unavailable():
    from fundamental_engine import score_fundamentals

    out = score_fundamentals({})
    assert out["available"] is False
    assert out["score"] is None


def test_score_fundamentals_weak_debt():
    from fundamental_engine import score_fundamentals

    row = {"roe": 3.0, "debt_to_equity": 3.5}
    out = score_fundamentals(row)
    assert out["available"] is True
    assert out["score"] < 50.0


def test_score_fundamentals_peg_and_fcf():
    from fundamental_engine import score_fundamentals

    row = {
        "roe": 14.0,
        "pe": 18.0,
        "eps_growth_pct": 12.0,
        "free_cash_flow": 500.0,
        "market_cap": 10000.0,
    }
    out = score_fundamentals(row)
    assert out["available"] is True
    assert out["metadata"].get("peg") is not None
    assert out["metadata"].get("fcf_yield_pct") == pytest.approx(5.0, abs=0.1)


def test_score_fundamentals_revenue_cagr_from_history():
    from fundamental_engine import score_fundamentals

    row = {"revenue_history": [100.0, 110.0, 125.0, 140.0]}
    out = score_fundamentals(row)
    assert out["available"] is True
    assert out["metadata"].get("revenue_cagr_pct") is not None


def test_score_fundamentals_missing_extended_metrics_skips():
    from fundamental_engine import score_fundamentals

    out = score_fundamentals({"roe": 12.0})
    assert out["available"] is True
    assert "peg" not in out["metadata"]
    assert "fcf_yield_pct" not in out["metadata"]
