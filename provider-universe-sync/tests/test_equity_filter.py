from __future__ import annotations

import os

import pytest

from src.equity_filter import is_meaningful_listed_equity
from src.models import UniverseInstrument


def _inst(**kwargs) -> UniverseInstrument:
    base = dict(
        exchange="NSE",
        symbol="RELIANCE",
        instrument_name="Reliance Industries Limited",
        isin="INE002A01018",
        official_sector_key="energy",
        official_industry="Oil",
    )
    base.update(kwargs)
    return UniverseInstrument(**base)


def test_keeps_valid_isin_equity():
    assert is_meaningful_listed_equity(_inst()) is True


def test_drops_bad_isin():
    assert is_meaningful_listed_equity(_inst(isin="")) is False
    assert is_meaningful_listed_equity(_inst(isin="IN123")) is False
    assert is_meaningful_listed_equity(_inst(isin="XX002A010188")) is False


def test_drops_name_markers():
    assert is_meaningful_listed_equity(_inst(instrument_name="NIFTY 50 INDEX")) is False
    assert is_meaningful_listed_equity(_inst(instrument_name="LIQUID ETF")) is False
    assert is_meaningful_listed_equity(_inst(instrument_name="FOO MUTUAL FUND PLAN")) is False


def test_relaxed_nse_letters_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EQUITY_FILTER_STRICT_ISIN", "0")
    assert is_meaningful_listed_equity(_inst(isin="", symbol="HDFCBANK", instrument_name="HDFC Bank")) is True
    assert is_meaningful_listed_equity(_inst(isin="", symbol="RELIANCE1", instrument_name="Test")) is False


def test_strict_default_requires_isin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EQUITY_FILTER_STRICT_ISIN", raising=False)
    # default strict
    assert is_meaningful_listed_equity(_inst(isin="", symbol="HDFCBANK")) is False
