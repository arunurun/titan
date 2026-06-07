"""Tests for live PM macro data fetch (yfinance mocked — no live API in CI)."""

from datetime import date, timedelta

import pandas as pd
import pytest

import pm_macro_data as pmd


def _make_yf_frame(ticker_closes: dict[str, list[float]], start: date) -> pd.DataFrame:
    """Build MultiIndex yfinance-style Close frame."""
    n = len(next(iter(ticker_closes.values())))
    idx = pd.bdate_range(start, periods=n)
    cols = pd.MultiIndex.from_product([["Close"], list(ticker_closes.keys())])
    data = {col: vals for col, vals in zip(ticker_closes.keys(), ticker_closes.values())}
    close = pd.DataFrame(data, index=idx)
    close.columns = pd.MultiIndex.from_product([["Close"], close.columns])
    return close


@pytest.fixture
def mock_yfinance(monkeypatch):
    start = date.today() - timedelta(days=400)
    n = 280

    def _gold():
        return [2000.0 + i * 0.1 for i in range(n)]

    def _silver():
        return [25.0 + i * 0.01 for i in range(n)]

    def _dxy():
        return [104.0 - i * 0.02 for i in range(n)]

    def _usdcny():
        return [7.2 + i * 0.0001 for i in range(n)]

    def _shau():
        # CNY/gram level that implies ~1% premium vs COMEX at ~$2000/oz, CNY 7.2
        return [465.0 + i * 0.05 for i in range(n)]

    core = {
        "GC=F": _gold(),
        "SI=F": _silver(),
        "DX-Y.NYB": _dxy(),
        "DX=F": _dxy(),
    }
    sge = {
        "SHAU.SHF": _shau(),
        "518880.SS": _shau(),
        "CNY=X": _usdcny(),
    }

    def fake_download(tickers, start=None, end=None, **kwargs):
        if isinstance(tickers, str):
            tickers = [tickers]
        subset = {t: (core if t in core else sge)[t] for t in tickers if t in core or t in sge}
        return _make_yf_frame(subset, start)

    import yfinance as yf

    monkeypatch.setattr(yf, "download", fake_download)
    return start


def test_fetch_pm_macro_series_shape_and_keys(mock_yfinance, tmp_path, monkeypatch):
    monkeypatch.setenv("TITAN_PM_MACRO_CSV", str(tmp_path / "pm_macro_series.csv"))
    data, meta = pmd.fetch_pm_macro_series(lookback_days=300)
    assert "GOLD" in data
    assert "SILVER" in data
    assert "DXY" in data
    assert len(data["GOLD"]) >= 252
    assert len(data["GOLD"]) == len(data["SILVER"]) == len(data["DXY"])
    assert meta["source"] == "yfinance"
    assert meta["tickers"]["GOLD"] == "GC=F"
    assert meta["tickers"]["SILVER"] == "SI=F"
    cache = tmp_path / "pm_macro_series.csv"
    assert cache.is_file()
    cached = pd.read_csv(cache)
    assert "GOLD" in cached.columns
    assert len(cached) >= 252


def test_fetch_pm_macro_series_derives_sge_premium(mock_yfinance):
    data, meta = pmd.fetch_pm_macro_series(lookback_days=300, write_cache=False)
    assert meta.get("sge_source") == "proxy"
    assert "SGE_PREMIUM_PCT" in data
    assert not data["SGE_PREMIUM_PCT"].dropna().empty


def test_fetch_pm_macro_series_raises_when_core_missing(monkeypatch):
    def empty_download(*args, **kwargs):
        return pd.DataFrame()

    import yfinance as yf

    monkeypatch.setattr(yf, "download", empty_download)
    with pytest.raises(RuntimeError, match="no data"):
        pmd.fetch_pm_macro_series(lookback_days=300, write_cache=False)


def test_load_pm_macro_series_live_default(mock_yfinance, tmp_path, monkeypatch):
    monkeypatch.delenv("TITAN_PM_LIVE_FETCH", raising=False)
    monkeypatch.setenv("TITAN_PM_MACRO_CSV", str(tmp_path / "cache.csv"))
    data, notes = pmd.load_pm_macro_series()
    assert data is not None
    assert "GOLD" in data
    assert any("SGE premium" in n or "SGE data" in n for n in notes)


def test_load_pm_macro_series_csv_when_live_disabled(monkeypatch, tmp_path):
    from precious_metals_algo import generate_synthetic_pm_macro_series

    fixture = tmp_path / "offline.csv"
    syn = generate_synthetic_pm_macro_series(n=35)
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=35, freq="B").strftime("%Y-%m-%d"),
            "GOLD": syn["GOLD"],
            "SILVER": syn["SILVER"],
            "DXY": syn["DXY"],
            "SGE_PREMIUM_PCT": syn["SGE_PREMIUM_PCT"],
            "SGE_WITHDRAWAL": syn["SGE_WITHDRAWAL"],
        }
    )
    df.to_csv(fixture, index=False)
    monkeypatch.setenv("TITAN_PM_LIVE_FETCH", "0")
    monkeypatch.setenv("TITAN_PM_MACRO_CSV", str(fixture))
    data, notes = pmd.load_pm_macro_series()
    assert data is not None
    assert len(data["GOLD"]) == 35
    assert any("TITAN_PM_LIVE_FETCH=0" in n for n in notes)


def test_pm_live_fetch_enabled():
    import os

    os.environ["TITAN_PM_LIVE_FETCH"] = "1"
    assert pmd.pm_live_fetch_enabled() is True
    os.environ["TITAN_PM_LIVE_FETCH"] = "0"
    assert pmd.pm_live_fetch_enabled() is False
    del os.environ["TITAN_PM_LIVE_FETCH"]
    assert pmd.pm_live_fetch_enabled() is True
