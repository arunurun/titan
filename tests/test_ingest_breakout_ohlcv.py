"""Tests for breakout OHLCV ingest helpers."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_ohlcv_store import ohlcv_dict_to_rows  # noqa: E402


def test_ohlcv_dict_to_rows_skips_invalid_bars():
    parsed = {
        "timestamp": [1_700_000_000, 1_700_086_400],
        "open": [0.0, 10.0],
        "high": [0.0, 11.0],
        "low": [0.0, 9.0],
        "close": [0.0, 10.5],
        "volume": [0, 500],
    }
    rows = ohlcv_dict_to_rows("ABC", parsed)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ABC"


def test_filter_new_rows_logic():
    from scripts.ingest_breakout_ohlcv import _filter_new_rows

    rows = [
        {"trade_date": "2026-01-01"},
        {"trade_date": "2026-01-02"},
        {"trade_date": "2026-01-03"},
    ]
    assert len(_filter_new_rows(rows, date(2026, 1, 1))) == 2
    assert len(_filter_new_rows(rows, None)) == 3


@patch("scripts.ingest_breakout_ohlcv.fetch_yahoo_data")
@patch("scripts.ingest_breakout_ohlcv._upsert")
def test_ingest_symbol_incremental(mock_upsert, mock_fetch):
    from scripts.ingest_breakout_ohlcv import ingest_symbol

    mock_fetch.return_value = (
        {
            "timestamp": [1_700_000_000, 1_700_950_000],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "volume": [100, 200],
        },
        None,
    )
    mock_upsert.return_value = (1, "ok")
    client = MagicMock()
    sym, n, status = ingest_symbol(
        client,
        "RELIANCE",
        "RELIANCE.NS",
        max_date=date(2026, 1, 1),
        full_backfill=False,
        dry_run=False,
        throttle=0.0,
    )
    assert sym == "RELIANCE"
    assert n == 1
    assert status == "ok"
    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.kwargs.get("skip_supabase") is True
