"""Live fetch for precious metals macro series (Yahoo Finance via yfinance)."""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_PM_MACRO_CACHE = Path("data/cache/pm_macro_series.csv")
_GRAMS_PER_TROY_OZ = 31.1034768

# Yahoo Finance tickers
_TICKER_GOLD = "GC=F"
_TICKER_SILVER = "SI=F"
_TICKER_DXY = "DX-Y.NYB"
_TICKER_DXY_FALLBACK = "DX=F"
_TICKER_USDCNY = "CNY=X"
# Shanghai gold benchmark (CNY/gram); 518880.SS = gold ETF (~0.01g Au per share)
_SGE_PROXY_TICKERS = ("SHAU.SHF", "518880.SS")
_ETF_GOLD_GRAMS_PER_SHARE: dict[str, float] = {"518880.SS": 0.01}

_REQUIRED_KEYS = ("GOLD", "SILVER", "DXY")


def pm_live_fetch_enabled() -> bool:
    """Return True unless TITAN_PM_LIVE_FETCH is explicitly disabled."""
    raw = os.environ.get("TITAN_PM_LIVE_FETCH", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _extract_close_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance download output to a date-indexed Close frame."""
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            close = raw["Close"].copy()
        elif "Adj Close" in raw.columns.get_level_values(0):
            close = raw["Adj Close"].copy()
        else:
            return pd.DataFrame()
    elif "Close" in raw.columns:
        close = raw[["Close"]].copy()
        close.columns = [raw.attrs.get("ticker", "Close") if hasattr(raw, "attrs") else "Close"]
    elif "Adj Close" in raw.columns:
        close = raw[["Adj Close"]].copy()
        close.columns = ["Close"]
    else:
        return pd.DataFrame()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.sort_index()


def _download_closes(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    if not tickers:
        return pd.DataFrame()
    unique = list(dict.fromkeys(tickers))
    try:
        raw = yf.download(
            unique,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
            group_by="column",
            threads=False,
        )
    except Exception as ex:
        raise RuntimeError(f"yfinance download failed for {unique}: {ex}") from ex
    return _extract_close_frame(raw)


def _series_from_frame(frame: pd.DataFrame, ticker: str) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float)
    if ticker in frame.columns:
        return pd.to_numeric(frame[ticker], errors="coerce").dropna()
    # Single-ticker downloads sometimes use a generic column name
    if len(frame.columns) == 1:
        return pd.to_numeric(frame.iloc[:, 0], errors="coerce").dropna()
    return pd.Series(dtype=float)


def _derive_sge_premium_pct(
    sge_proxy: pd.Series,
    gold_usd: pd.Series,
    usdcny: pd.Series,
    proxy_ticker: str,
) -> pd.Series:
    """Derive Shanghai vs COMEX premium (%) from a China gold proxy."""
    merged = pd.DataFrame({"sge": sge_proxy, "gold": gold_usd, "usdcny": usdcny}).dropna()
    if merged.empty:
        return pd.Series(dtype=float)

    if proxy_ticker.upper().startswith("SHAU"):
        # SHAU.SHF: Shanghai benchmark, CNY per gram → USD per troy oz
        sge_usd_oz = (merged["sge"] / merged["usdcny"]) * _GRAMS_PER_TROY_OZ
    elif proxy_ticker in _ETF_GOLD_GRAMS_PER_SHARE:
        grams = _ETF_GOLD_GRAMS_PER_SHARE[proxy_ticker]
        cny_per_gram = merged["sge"] / grams
        sge_usd_oz = (cny_per_gram / merged["usdcny"]) * _GRAMS_PER_TROY_OZ
    else:
        return pd.Series(dtype=float)

    gold = merged["gold"].replace(0.0, float("nan"))
    premium = ((sge_usd_oz / gold) - 1.0) * 100.0
    return premium.dropna()


def fetch_pm_macro_series(
    lookback_days: int = 300,
    *,
    write_cache: bool = True,
) -> tuple[dict[str, pd.Series], dict[str, Any]]:
    """
    Fetch aligned PM macro series from Yahoo Finance.

    Returns (data, meta). Keys: GOLD, SILVER, DXY; optional SGE_PREMIUM_PCT,
    SGE_WITHDRAWAL (NaN when no live source). GSR is computed downstream from GOLD/SILVER.
    """
    if lookback_days < 20:
        raise ValueError("lookback_days must be >= 20")

    end = date.today()
    start = end - timedelta(days=lookback_days + 60)

    core_tickers = [_TICKER_GOLD, _TICKER_SILVER, _TICKER_DXY, _TICKER_DXY_FALLBACK]
    frame = _download_closes(core_tickers, start, end)
    if frame.empty:
        raise RuntimeError("yfinance returned no data for GOLD/SILVER/DXY")

    gold = _series_from_frame(frame, _TICKER_GOLD)
    silver = _series_from_frame(frame, _TICKER_SILVER)
    dxy = _series_from_frame(frame, _TICKER_DXY)
    if dxy.empty:
        dxy = _series_from_frame(frame, _TICKER_DXY_FALLBACK)

    if gold.empty or silver.empty or dxy.empty:
        missing = [
            name
            for name, series in (("GOLD", gold), ("SILVER", silver), ("DXY", dxy))
            if series.empty
        ]
        raise RuntimeError(f"Missing required PM macro series after fetch: {', '.join(missing)}")

    aligned = pd.DataFrame({"GOLD": gold, "SILVER": silver, "DXY": dxy}).dropna()
    if len(aligned) < 20:
        raise RuntimeError(
            f"Insufficient overlapping PM macro rows ({len(aligned)}); need at least 20"
        )

    meta: dict[str, Any] = {
        "dates": aligned.index,
        "source": "yfinance",
        "tickers": {
            "GOLD": _TICKER_GOLD,
            "SILVER": _TICKER_SILVER,
            "DXY": _TICKER_DXY if not _series_from_frame(frame, _TICKER_DXY).empty else _TICKER_DXY_FALLBACK,
        },
        "sge_source": None,
        "sge_unavailable": False,
    }

    out: dict[str, pd.Series] = {
        key: aligned[key].reset_index(drop=True) for key in _REQUIRED_KEYS
    }

    # SGE premium proxy (optional); withdrawals not available from free Yahoo feeds
    sge_tickers = list(_SGE_PROXY_TICKERS) + [_TICKER_USDCNY]
    sge_frame = _download_closes(sge_tickers, start, end)
    usdcny = _series_from_frame(sge_frame, _TICKER_USDCNY)
    premium: pd.Series = pd.Series(dtype=float)
    proxy_used: str | None = None

    if not usdcny.empty:
        gold_idx = gold.copy()
        gold_idx.index = pd.to_datetime(gold_idx.index)
        for proxy_ticker in _SGE_PROXY_TICKERS:
            sge_proxy = _series_from_frame(sge_frame, proxy_ticker)
            if sge_proxy.empty:
                continue
            sge_proxy.index = pd.to_datetime(sge_proxy.index)
            premium = _derive_sge_premium_pct(sge_proxy, gold_idx, usdcny, proxy_ticker)
            if not premium.empty:
                proxy_used = proxy_ticker
                break

    if not premium.empty:
        # Align premium length to core aligned frame (positional tail match)
        premium_vals = premium.reset_index(drop=True)
        if len(premium_vals) >= len(out["GOLD"]):
            out["SGE_PREMIUM_PCT"] = premium_vals.iloc[-len(out["GOLD"]) :].reset_index(drop=True)
        else:
            pad = len(out["GOLD"]) - len(premium_vals)
            out["SGE_PREMIUM_PCT"] = pd.concat(
                [pd.Series([float("nan")] * pad), premium_vals], ignore_index=True
            )
        meta["sge_source"] = "proxy"
        meta["sge_proxy_ticker"] = proxy_used
    else:
        meta["sge_unavailable"] = True
        logger.warning("SGE premium proxy unavailable; SGE overlay will be neutral")

    if write_cache:
        _write_pm_macro_cache(out, aligned.index)

    return out, meta


def _write_pm_macro_cache(data: dict[str, pd.Series], dates: pd.DatetimeIndex) -> None:
    """Overwrite audit cache CSV after a successful live fetch."""
    cache_path = Path(os.environ.get("TITAN_PM_MACRO_CSV", str(_DEFAULT_PM_MACRO_CACHE)))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame({"date": pd.to_datetime(dates).strftime("%Y-%m-%d")})
        for key, series in data.items():
            df[key] = series.values
        df.to_csv(cache_path, index=False)
        logger.info("Wrote PM macro cache to %s (%d rows)", cache_path, len(df))
    except Exception as ex:
        logger.warning("Failed to write PM macro cache %s: %s", cache_path, ex)


def _pm_notes_from_meta(meta: dict[str, Any], *, live: bool) -> list[str]:
    notes: list[str] = []
    if live and meta.get("sge_source") == "proxy":
        proxy = meta.get("sge_proxy_ticker", "Shanghai proxy")
        notes.append(
            f"Note: SGE premium from {proxy} vs COMEX proxy (live); withdrawals unavailable live"
        )
    elif live and meta.get("sge_unavailable"):
        notes.append("Note: SGE data unavailable this run — physical-demand overlay neutral")
    elif not live:
        notes.append("Note: using cached CSV macro data (TITAN_PM_LIVE_FETCH=0 or live fetch failed)")
    return notes


def load_pm_macro_series() -> tuple[dict[str, pd.Series] | None, list[str]]:
    """
    Load PM macro data: live Yahoo fetch by default, CSV fallback on failure.

    Returns (data dict or None, optional email note lines).
    """
    notes: list[str] = []
    live_attempted = pm_live_fetch_enabled()
    if live_attempted:
        try:
            data, meta = fetch_pm_macro_series()
            notes.extend(_pm_notes_from_meta(meta, live=True))
            return data, notes
        except Exception as ex:
            logger.warning("PM macro live fetch failed, trying CSV fallback: %s", ex)
            notes.append(f"Note: live macro fetch failed ({ex}); using CSV fallback")

    from precious_metals_algo import load_pm_macro_series_from_csv

    data = load_pm_macro_series_from_csv()
    if data is None:
        return None, notes
    if not live_attempted:
        notes.append("Note: using cached CSV macro data (TITAN_PM_LIVE_FETCH=0)")
    return data, notes
