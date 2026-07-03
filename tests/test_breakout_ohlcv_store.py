"""Tests for breakout OHLCV Supabase store."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_ohlcv_store import (  # noqa: E402
    clear_bulk_cache,
    is_recent_enough,
    load_ohlcv_bulk_from_supabase,
    load_ohlcv_from_supabase,
    ohlcv_dict_to_rows,
    reset_ohlcv_stats,
    rows_to_ohlcv_dict,
)
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _sample_rows(n: int = 55, *, last_day: date | None = None) -> list[dict]:
    if last_day is None:
        last_day = datetime.now(IST).date()
    rows = []
    d = last_day
    while len(rows) < n:
        if d.weekday() < 5:
            i = len(rows)
            rows.append(
                {
                    "symbol": "RELIANCE",
                    "trade_date": d.isoformat(),
                    "open": 100.0 + i,
                    "high": 101.0 + i,
                    "low": 99.0 + i,
                    "close": 100.5 + i,
                    "volume": 1000 + i,
                }
            )
        d -= timedelta(days=1)
    rows.reverse()
    return rows


def test_rows_to_ohlcv_dict_sorted_and_lengths():
    rows = _sample_rows(10)
    parsed = rows_to_ohlcv_dict(rows)
    assert len(parsed["close"]) == len(rows)
    assert parsed["timestamp"] == sorted(parsed["timestamp"])


def test_ohlcv_dict_to_rows_roundtrip():
    rows = _sample_rows(5)
    parsed = rows_to_ohlcv_dict(rows)
    out = ohlcv_dict_to_rows("reliance", parsed)
    assert len(out) == 5
    assert out[0]["symbol"] == "RELIANCE"
    assert "trade_date" in out[0]


def test_is_recent_enough_within_three_trading_days():
    today = datetime.now(IST).date()
    assert is_recent_enough(today, max_stale_trading_days=3) is True


def test_load_ohlcv_from_supabase_mock_client(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    rows = _sample_rows(55)
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=rows
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")

    parsed, err = load_ohlcv_from_supabase("RELIANCE", client=mock_client)
    assert err is None
    assert parsed is not None
    assert len(parsed["close"]) >= 50


def test_load_ohlcv_from_supabase_rejects_stale(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    old = datetime.now(IST).date() - timedelta(days=30)
    rows = _sample_rows(55, last_day=old)
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=rows
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")

    parsed, err = load_ohlcv_from_supabase("RELIANCE", client=mock_client)
    assert parsed is None
    assert err is not None
    assert "stale" in err


def test_load_ohlcv_bulk_primes_cache(monkeypatch):
    reset_ohlcv_stats()
    clear_bulk_cache()
    rows = _sample_rows(55)
    for r in rows:
        r["symbol"] = "TCS"
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=rows
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")

    bulk = load_ohlcv_bulk_from_supabase(["TCS"], client=mock_client)
    assert "TCS" in bulk
    parsed, err = load_ohlcv_from_supabase("TCS")
    assert err is None
    assert parsed is not None
