"""Tests for breakout EOD context loaders and NSE shareholding normalization."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import nse_eod  # noqa: E402
from breakout_eod_context import load_free_float_pct_by_symbol  # noqa: E402


class _FakeExecute:
    def __init__(self, data: list[dict]):
        self.data = data


class _FakeQuery:
    def __init__(self, data: list[dict]):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def lte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _FakeExecute(self._data)


def _mock_supabase_client(rows: list[dict]):
    client = MagicMock()
    client.table.return_value = _FakeQuery(rows)
    return client


def test_normalize_shareholding_rows_maps_public_val():
    raw = [
        {
            "symbol": "RELIANCE",
            "date": "31-Mar-2025",
            "pr_and_prgrp": "50.12",
            "public_val": "49.88",
            "submissionDate": "14-Apr-2025 18:30:00",
        }
    ]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["as_of_date"] == "2025-03-31"
    assert rows[0]["free_float_pct"] == 49.88
    assert rows[0]["promoter_holding_pct"] == 50.12
    assert rows[0]["source"] == "nse_corporate_share_holdings_master"


def test_normalize_shareholding_rows_falls_back_to_promoter():
    raw = [
        {
            "symbol": "TCS",
            "date": "30-Jun-2025",
            "pr_and_prgrp": "72.5",
            "submissionDate": "10-Jul-2025 12:00:00",
        }
    ]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert len(rows) == 1
    assert rows[0]["free_float_pct"] == 27.5
    assert rows[0]["promoter_holding_pct"] == 72.5


def test_normalize_shareholding_rows_prefers_latest_submission():
    raw = [
        {
            "symbol": "INFY",
            "date": "31-Mar-2025",
            "public_val": "40.0",
            "submissionDate": "01-Apr-2025 10:00:00",
        },
        {
            "symbol": "INFY",
            "date": "31-Mar-2025",
            "public_val": "41.5",
            "submissionDate": "15-Apr-2025 10:00:00",
        },
    ]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert len(rows) == 1
    assert rows[0]["free_float_pct"] == 41.5


def test_load_free_float_pct_by_symbol_returns_latest_on_or_before_as_of():
    rows = [
        {"symbol": "RELIANCE", "as_of_date": "2025-03-31", "free_float_pct": 49.88},
        {"symbol": "RELIANCE", "as_of_date": "2024-12-31", "free_float_pct": 48.0},
        {"symbol": "TCS", "as_of_date": "2025-06-30", "free_float_pct": 27.5},
    ]
    client = _mock_supabase_client(rows)
    with patch("breakout_eod_context._supabase_client", return_value=client):
        out = load_free_float_pct_by_symbol(
            ["RELIANCE", "TCS", "HDFCBANK"], as_of_date=date(2025, 7, 1)
        )
    assert out["RELIANCE"] == 49.88
    assert out["TCS"] == 27.5
    assert out["HDFCBANK"] is None
    client.table.assert_called_once_with("shareholding_quarterly")


def test_load_free_float_pct_by_symbol_no_client_returns_none():
    with patch("breakout_eod_context._supabase_client", return_value=None):
        out = load_free_float_pct_by_symbol(["RELIANCE"])
    assert out == {"RELIANCE": None}


def test_load_free_float_pct_by_symbol_missing_table_returns_none():
    from postgrest.exceptions import APIError

    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.lte.return_value.order.return_value.limit.return_value.execute.side_effect = APIError(
        {"message": "Could not find the table 'public.shareholding_quarterly'", "code": "PGRST205"}
    )
    with patch("breakout_eod_context._supabase_client", return_value=client):
        out = load_free_float_pct_by_symbol(["RELIANCE"])
    assert out == {"RELIANCE": None}
