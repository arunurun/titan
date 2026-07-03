"""Unit tests for breadth_engine."""

from __future__ import annotations

import pandas as pd
import pytest


def _ohlc(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_compute_market_breadth_bullish_panel():
    from breadth_engine import compute_market_breadth

    panel = {
        "A": _ohlc([100 + i for i in range(250)]),
        "B": _ohlc([200 + i for i in range(250)]),
        "C": _ohlc([50 + i * 0.5 for i in range(250)]),
    }
    metrics = compute_market_breadth(panel)
    assert metrics["n_symbols"] == 3
    assert metrics["pct_above_ema200"] == pytest.approx(100.0)
    assert metrics["advance_decline_ratio"] is not None


def test_score_breadth():
    from breadth_engine import score_breadth

    out = score_breadth({"n_symbols": 50, "pct_above_ema200": 62.0, "pct_above_ema50": 58.0})
    assert out["available"] is True
    assert out["score"] == pytest.approx(60.0, abs=1.0)
    assert out["metadata"].get("diagnostic") is True


def test_stamp_macro_context_nifty():
    from breadth_engine import stamp_macro_context

    closes = [100.0 + i * 0.1 for i in range(220)]
    nifty_df = pd.DataFrame({"close": closes})
    ctx = stamp_macro_context(nifty_df=nifty_df, macro_snapshot={"india_vix": 14.5})
    assert ctx["nifty_above_ema200"] is True
    assert ctx["india_vix"] == 14.5
    assert "nifty_ema200" in ctx
