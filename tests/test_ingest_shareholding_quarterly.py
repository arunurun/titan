"""Tests for shareholding quarterly ingest parsing."""

from __future__ import annotations

import nse_eod


def test_normalize_shareholding_rows_maps_public_val_to_free_float():
    raw = [
        {
            "symbol": "reliance",
            "date": "31-Mar-2026",
            "pr_and_prgrp": "50.25",
            "public_val": "49.75",
            "submissionDate": "15-Apr-2026",
        }
    ]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["as_of_date"] == "2026-03-31"
    assert rows[0]["free_float_pct"] == 49.75
    assert rows[0]["promoter_holding_pct"] == 50.25
    assert rows[0]["source"] == "nse_corporate_share_holdings_master"


def test_normalize_shareholding_rows_derives_free_float_from_promoter():
    raw = [{"symbol": "TCS", "date": "2026-03-31", "pr_and_prgrp": "72.5"}]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert rows[0]["free_float_pct"] == 27.5


def test_normalize_shareholding_rows_prefers_latest_submission():
    raw = [
        {
            "symbol": "INFY",
            "date": "31-Dec-2025",
            "public_val": "40.0",
            "submissionDate": "01-Feb-2026",
        },
        {
            "symbol": "INFY",
            "date": "31-Dec-2025",
            "public_val": "41.5",
            "submissionDate": "15-Feb-2026",
        },
    ]
    rows = nse_eod.normalize_shareholding_rows(raw)
    assert len(rows) == 1
    assert rows[0]["free_float_pct"] == 41.5


def test_normalize_shareholding_rows_skips_invalid_rows():
    rows = nse_eod.normalize_shareholding_rows(
        [
            {"symbol": "", "date": "31-Mar-2026", "public_val": "10"},
            {"symbol": "ABC", "date": "bad-date", "public_val": "10"},
            {"symbol": "XYZ", "date": "31-Mar-2026", "public_val": "-1"},
        ]
    )
    assert rows == []


def test_fetch_shareholding_master_delegates_to_download_json(monkeypatch):
    captured: dict[str, str] = {}

    def _fake(path, referer=""):
        captured["path"] = path
        captured["referer"] = referer
        return [{"symbol": "HDFCBANK", "date": "31-Mar-2026", "public_val": "45"}]

    monkeypatch.setattr(nse_eod, "_download_json", _fake)
    from datetime import date

    out = nse_eod.fetch_shareholding_master(
        from_date=date(2026, 1, 1), to_date=date(2026, 3, 31), symbol="hdfcbank"
    )
    assert len(out) == 1
    assert "corporate-share-holdings-master" in captured["path"]
    assert "from_date=01-01-2026" in captured["path"]
    assert "symbol=HDFCBANK" in captured["path"]
