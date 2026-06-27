from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_store import (  # noqa: E402
    build_analysis_record,
    persist_breakout_stock_analysis,
)


class _Cfg:
    supabase_url = "https://x.supabase.co"
    supabase_key = "service"


class _CfgMissing:
    supabase_url = ""
    supabase_key = ""


def test_build_analysis_record_pass_mapping():
    row = build_analysis_record(
        run_id="11111111-2222-3333-4444-555555555555",
        scan_date="2026-06-26",
        ticker="RELIANCE",
        tier="Small-Cap (Nifty Smallcap 100)",
        symbol_yahoo="RELIANCE.NS",
        bar_count=252,
        latest_close=101.5,
        prev_close=98.0,
        pct_change=3.5714,
        latest_volume=1_250_000.0,
        vol_20_avg=400_000.0,
        vol_mult=3.125,
        rsi_14=58.2,
        adx_14=28.5,
        sma_50=95.0,
        sma_200=88.0,
        poc_30d=99.5,
        min_price_threshold=15.0,
        vol_mult_threshold=3.5,
        price_above_sma50=True,
        yahoo_as_of_date="2026-06-25",
        passed=True,
        fail_reason=None,
        entry_low=100.0,
        entry_high=102.5,
        stop_loss=93.1,
        target_price=118.3,
        target_gain_pct=16.55,
        signal_tier="PASS",
        persistence_score=3,
        composite_rank=82.5,
        liquidity_quality=71.0,
        breakout_stage=1,
        base_score=65.0,
        pass_paths="vol_cum",
        risk_flags="",
        inserted_at="2026-06-26T09:30:00+05:30",
    )
    assert row["run_id"] == "11111111-2222-3333-4444-555555555555"
    assert row["scan_date"] == "2026-06-26"
    assert row["inserted_at"] == "2026-06-26T09:30:00+05:30"
    assert row["ticker"] == "RELIANCE"
    assert row["tier"] == "Small-Cap (Nifty Smallcap 100)"
    assert row["symbol_yahoo"] == "RELIANCE.NS"
    assert row["bar_count"] == 252
    assert row["latest_close"] == 101.5
    assert row["pct_change"] == 3.5714
    assert row["vol_mult"] == 3.125
    assert row["rsi_14"] == 58.2
    assert row["adx_14"] == 28.5
    assert row["price_above_sma50"] is True
    assert row["passed"] is True
    assert row["fail_reason"] is None
    assert row["entry_low"] == 100.0
    assert row["entry_high"] == 102.5
    assert row["stop_loss"] == 93.1
    assert row["target_price"] == 118.3
    assert row["target_gain_pct"] == 16.55
    assert row["signal_tier"] == "PASS"
    assert row["persistence_score"] == 3
    assert row["composite_rank"] == 82.5
    assert row["liquidity_quality"] == 71.0
    assert row["breakout_stage"] == 1
    assert row["base_score"] == 65.0
    assert row["pass_paths"] == "vol_cum"
    assert row["risk_flags"] == ""


def test_build_analysis_record_fail_mapping_strips_ns_suffix():
    row = build_analysis_record(
        run_id="run-1",
        scan_date="2026-06-26",
        ticker="ABC.NS",
        tier="Micro-Cap (Nifty Microcap 250)",
        symbol_yahoo="ABC.NS",
        fetch_error="HTTP 429",
        bar_count=12,
        passed=False,
        fail_reason="insufficient_data (HTTP 429)",
    )
    assert row["ticker"] == "ABC"
    assert row["fetch_error"] == "HTTP 429"
    assert row["passed"] is False
    assert row["fail_reason"] == "insufficient_data (HTTP 429)"
    assert row["entry_low"] is None
    assert row["target_gain_pct"] is None


def test_persist_breakout_stock_analysis_skips_without_supabase_config():
    out = persist_breakout_stock_analysis(_CfgMissing(), [{"ticker": "ABC"}])
    assert out["configured"] is False
    assert out["persisted"] is False
    assert out["reason"] == "missing_config"


def test_persist_breakout_stock_analysis_upserts():
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.upsert.return_value.execute.return_value = MagicMock(data=[{"ticker": "ABC"}])

    records = [{"run_id": "run-1", "ticker": "ABC", "scan_date": "2026-06-26"}]
    with patch("breakout_store.create_client", return_value=mock_client):
        out = persist_breakout_stock_analysis(_Cfg(), records)

    assert out["configured"] is True
    assert out["persisted"] is True
    assert out["rows"] == 1
    mock_client.table.assert_called_once_with("breakout_stock_analysis")
    mock_table.upsert.assert_called_once()
    kwargs = mock_table.upsert.call_args
    assert kwargs.kwargs["on_conflict"] == "run_id,ticker"
