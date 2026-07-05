"""Tests for breakout backtest Supabase OHLCV integration."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_backtest import (  # noqa: E402
    _fetch_one_symbol_history,
    _resolve_prefer_supabase,
    fetch_universe_history,
)
from breakout_ohlcv_store import clear_bulk_cache, reset_ohlcv_stats  # noqa: E402


def _universe_entry(sym: str = "RELIANCE") -> dict:
    return {
        "symbol": sym,
        "yahoo_ticker": f"{sym}.NS",
        "tier_key": "SMALL_CAP_100",
        "tier_label": "Small Cap",
    }


def _ohlcv_dict(n: int = 60) -> dict:
    return {
        "timestamp": list(range(n)),
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    }


def test_resolve_prefer_supabase_defaults_to_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert _resolve_prefer_supabase(None) is False

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "key")
    assert _resolve_prefer_supabase(None) is True
    assert _resolve_prefer_supabase(False) is False
    assert _resolve_prefer_supabase(True) is True


def test_fetch_one_symbol_history_uses_supabase(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    df = _ohlcv_dict()
    monkeypatch.setattr(
        "breakout_backtest._ohlcv_store",
        lambda: MagicMock(
            load_ohlcv_from_supabase=MagicMock(return_value=(df, None)),
            record_yahoo_fetch=MagicMock(),
        ),
    )
    yahoo = MagicMock(return_value=(None, "should_not_call"))
    monkeypatch.setattr("breakout_backtest.fetch_yahoo_history", yahoo)

    out, err, source = _fetch_one_symbol_history(
        _universe_entry(), range_str="3m", prefer_supabase=True,
    )
    assert err is None
    assert source == "supabase"
    assert len(out["close"]) == 60
    yahoo.assert_not_called()


def test_fetch_one_symbol_history_falls_back_to_yahoo(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    df = _ohlcv_dict()
    record = MagicMock()
    monkeypatch.setattr(
        "breakout_backtest._ohlcv_store",
        lambda: MagicMock(
            load_ohlcv_from_supabase=MagicMock(return_value=(None, "only 1 bars")),
            record_yahoo_fetch=record,
        ),
    )
    monkeypatch.setattr(
        "breakout_backtest.fetch_yahoo_history",
        MagicMock(return_value=(df, None)),
    )

    out, err, source = _fetch_one_symbol_history(
        _universe_entry(), range_str="3m", prefer_supabase=True,
    )
    assert err is None
    assert source == "yahoo"
    assert len(out["close"]) == 60
    record.assert_called_once()


def test_fetch_universe_history_supabase_manifest(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    df = _ohlcv_dict()
    store = MagicMock()
    store.is_supabase_configured.return_value = True
    store.reset_ohlcv_stats = reset_ohlcv_stats
    store.clear_bulk_cache = clear_bulk_cache
    store.load_ohlcv_bulk_from_supabase.return_value = {"RELIANCE": df}
    store.load_ohlcv_from_supabase.return_value = (df, None)
    store.get_ohlcv_stats.return_value = {"supabase_hits": 1, "yahoo_fetches": 0}
    store.record_yahoo_fetch = MagicMock()
    monkeypatch.setattr("breakout_backtest._ohlcv_store", lambda: store)
    monkeypatch.setattr("breakout_backtest.warm_yahoo_session", MagicMock())

    data, manifest = fetch_universe_history(
        [_universe_entry()],
        range_str="3m",
        prefer_supabase=True,
        warm_session=True,
    )
    assert "RELIANCE" in data
    assert manifest["prefer_supabase"] is True
    assert manifest["ohlcv_stats"]["supabase_hits"] == 1
    assert manifest["success"][0]["ohlcv_source"] == "supabase"
