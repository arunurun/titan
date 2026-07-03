"""Unit tests for relative_strength factor."""

from __future__ import annotations

import math

import pandas as pd
import pytest


def _trend(n: int, daily_pct: float = 0.3) -> pd.Series:
    base = 100.0
    vals = [base * ((1 + daily_pct / 100) ** i) for i in range(n)]
    return pd.Series(vals)


def test_score_relative_strength_outperformer():
    from relative_strength import score_relative_strength

    stock = _trend(120, 0.5)
    nifty = _trend(120, 0.1)
    sector = _trend(120, 0.2)
    out = score_relative_strength(stock, nifty, sector, horizons=(20, 50, 90))
    assert out["available"] is True
    assert out["score"] is not None
    assert out["score"] > 55.0
    meta = out["metadata"]
    assert meta["rel_return_20d_vs_nifty_pct"] is not None
    assert meta["rel_return_20d_vs_sector_pct"] is not None


def test_score_relative_strength_insufficient_data():
    from relative_strength import score_relative_strength

    out = score_relative_strength([], None, None)
    assert out["available"] is False
    assert out["score"] is None


def test_score_relative_strength_from_audit():
    from relative_strength import score_relative_strength_from_audit

    audit = {
        "rel_return_20d_vs_nifty_pct": 4.0,
        "rel_return_50d_vs_nifty_pct": 3.0,
        "sector_relative_strength_pctile": 82.0,
    }
    out = score_relative_strength_from_audit(audit)
    assert out["available"] is True
    assert out["score"] == pytest.approx(67.33, abs=1.5)


def test_cohort_percentile_rank():
    from relative_strength import score_relative_strength_from_audit

    cohort = [{"rel_return_20d_vs_nifty_pct": v} for v in (1.0, 2.0, 3.0, 8.0)]
    audit = {"rel_return_20d_vs_nifty_pct": 8.0}
    out = score_relative_strength_from_audit(audit, cohort_audits=cohort)
    assert out["available"] is True
    assert out["metadata"].get("cohort_rank_pctile") == 100.0
