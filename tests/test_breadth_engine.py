"""Unit tests for breadth_engine."""

from __future__ import annotations

import time
from types import SimpleNamespace

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


def test_prefetch_breadth_panel_batch_uses_concurrency(monkeypatch):
    from breadth_engine import prefetch_breadth_panel_batch

    calls: list[str] = []

    def _fake_fetch(cfg, symbol, exchange, *, breeze=None, lookback_calendar_days=280):
        calls.append(symbol)
        time.sleep(0.02)
        return pd.DataFrame({"close": [1.0, 2.0, 3.0]})

    monkeypatch.setattr("breeze_client.fetch_equity_data", _fake_fetch)
    instruments = [SimpleNamespace(symbol=f"S{i}", exchange="NSE") for i in range(8)]
    t0 = time.perf_counter()
    panel = prefetch_breadth_panel_batch(
        object(),
        object(),
        instruments,
        max_symbols=8,
        max_workers=4,
    )
    elapsed = time.perf_counter() - t0
    assert len(panel) == 8
    assert len(calls) == 8
    assert elapsed < 0.12
