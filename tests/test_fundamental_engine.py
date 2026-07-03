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
