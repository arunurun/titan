"""Tests for NSE delivery enrichment via breakout_eod_context."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd


def test_load_delivery_pct_by_symbol_from_bhavcopy():
    from breakout_eod_context import load_delivery_pct_by_symbol

    frame = pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TCS"],
            "deliv_per": [45.5, 38.2],
        }
    )

    with patch("nse_eod.fetch_sec_bhavdata_full", return_value=frame):
        out = load_delivery_pct_by_symbol(["RELIANCE", "TCS"], as_of_date="2026-06-30")

    assert out["RELIANCE"] == 45.5
    assert out["TCS"] == 38.2


def test_enrich_audit_sets_delivery_pct(monkeypatch):
    from institutional_flow import enrich_audit_institutional_data

    monkeypatch.setattr(
        "breakout_eod_context.load_delivery_pct_by_symbol",
        lambda symbols, **kw: {"INFY": 42.0},
    )

    class _Inst:
        symbol = "INFY"
        exchange = "NSE"

    audit: dict = {}
    flow = enrich_audit_institutional_data(audit, None, _Inst(), as_of_date="2026-06-30")
    assert audit.get("delivery_pct") == 42.0
    assert flow.get("available") is True
