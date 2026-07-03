"""Supabase-backed daily OHLCV cache for the breakout scanner."""

from __future__ import annotations

import datetime
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

TABLE = "equity_ohlcv_daily"
_DEFAULT_MIN_BARS = 50
_DEFAULT_MAX_STALE_TRADING_DAYS = 3

_BULK_CACHE: dict[str, dict[str, Any]] = {}
_stats: dict[str, int] = {"supabase_hits": 0, "yahoo_fetches": 0}


def reset_ohlcv_stats() -> None:
    _stats["supabase_hits"] = 0
    _stats["yahoo_fetches"] = 0


def get_ohlcv_stats() -> dict[str, int]:
    return dict(_stats)


def record_yahoo_fetch() -> None:
    _stats["yahoo_fetches"] += 1


def clear_bulk_cache() -> None:
    _BULK_CACHE.clear()


def is_supabase_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL", "").strip() and os.environ.get("SUPABASE_KEY", "").strip())


def _supabase_client() -> Any | None:
    if not is_supabase_configured():
        return None
    return create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_KEY"].strip(),
    )


def _trade_date_to_timestamp(trade_date: str | date) -> int:
    if isinstance(trade_date, date):
        d = trade_date
    else:
        d = date.fromisoformat(str(trade_date)[:10])
    dt_ist = datetime(d.year, d.month, d.day, tzinfo=IST)
    return int(dt_ist.timestamp())


def _subtract_trading_days(from_day: date, n: int) -> date:
    """Walk backward ``n`` NSE-style trading days (Mon–Fri)."""
    cur = from_day
    remaining = n
    while remaining > 0:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            remaining -= 1
    return cur


def is_recent_enough(last_trade_date: str | date, *, max_stale_trading_days: int = _DEFAULT_MAX_STALE_TRADING_DAYS) -> bool:
    if isinstance(last_trade_date, str):
        last = date.fromisoformat(last_trade_date[:10])
    else:
        last = last_trade_date
    today = datetime.now(IST).date()
    cutoff = _subtract_trading_days(today, max_stale_trading_days)
    return last >= cutoff


def rows_to_ohlcv_dict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert Supabase rows to the dict format expected by ``evaluate_bars_as_of``."""
    if not rows:
        return {
            "timestamp": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        }
    sorted_rows = sorted(rows, key=lambda r: str(r.get("trade_date") or ""))
    timestamps: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[int] = []
    for row in sorted_rows:
        td = row.get("trade_date")
        if not td:
            continue
        try:
            o = float(row["open"])
            h = float(row["high"])
            lo = float(row["low"])
            c = float(row["close"])
            vol = int(float(row.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        timestamps.append(_trade_date_to_timestamp(td))
        opens.append(o)
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        volumes.append(vol)
    return {
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }


def ohlcv_dict_to_audit_dataframe(parsed: dict[str, Any]) -> Any:
    """Convert Supabase OHLCV dict to a Breeze-like DataFrame for sector audits."""
    import pandas as pd

    timestamps = list(parsed.get("timestamp") or [])
    if not timestamps:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        try:
            dt = datetime.fromtimestamp(int(ts), tz=IST)
            rows.append(
                {
                    "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(parsed["open"][i]),
                    "high": float(parsed["high"][i]),
                    "low": float(parsed["low"][i]),
                    "close": float(parsed["close"][i]),
                    "volume": int(float(parsed["volume"][i])),
                }
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return pd.DataFrame(rows)


def ohlcv_dict_to_rows(symbol: str, parsed: dict[str, Any], *, source: str = "yahoo") -> list[dict[str, Any]]:
    """Convert Yahoo chart dict to Supabase upsert rows."""
    sym = str(symbol).strip().upper()
    rows: list[dict[str, Any]] = []
    timestamps = parsed.get("timestamp") or []
    opens = parsed.get("open") or []
    highs = parsed.get("high") or []
    lows = parsed.get("low") or []
    closes = parsed.get("close") or []
    volumes = parsed.get("volume") or []
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes), len(volumes))
    for i in range(n):
        try:
            td = datetime.fromtimestamp(int(timestamps[i]), tz=IST).date().isoformat()
            o = float(opens[i])
            h = float(highs[i])
            lo = float(lows[i])
            c = float(closes[i])
            vol = int(float(volumes[i] or 0))
        except (TypeError, ValueError):
            continue
        if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
            continue
        rows.append(
            {
                "symbol": sym,
                "trade_date": td,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": vol,
                "source": source,
            }
        )
    return rows


def _validate_cached(parsed: dict[str, Any], *, min_bars: int, max_stale_trading_days: int) -> str | None:
    if len(parsed.get("close") or []) < min_bars:
        return f"only {len(parsed.get('close') or [])} bars"
    last_ts = (parsed.get("timestamp") or [None])[-1]
    if last_ts is None:
        return "no_timestamp"
    last_day = datetime.fromtimestamp(int(last_ts), tz=IST).date()
    if not is_recent_enough(last_day, max_stale_trading_days=max_stale_trading_days):
        return f"stale last_bar={last_day.isoformat()}"
    return None


def load_ohlcv_from_supabase(
    symbol: str,
    *,
    min_bars: int = _DEFAULT_MIN_BARS,
    max_stale_trading_days: int = _DEFAULT_MAX_STALE_TRADING_DAYS,
    client: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load OHLCV for one NSE symbol from Supabase when sufficient and recent."""
    sym = str(symbol).strip().upper().replace(".NS", "")
    if sym in _BULK_CACHE:
        parsed = _BULK_CACHE[sym]
        err = _validate_cached(parsed, min_bars=min_bars, max_stale_trading_days=max_stale_trading_days)
        if err:
            return None, err
        _stats["supabase_hits"] += 1
        return parsed, None

    sb = client if client is not None else _supabase_client()
    if sb is None:
        return None, "supabase_not_configured"

    try:
        res = (
            sb.table(TABLE)
            .select("trade_date,open,high,low,close,volume")
            .eq("symbol", sym)
            .order("trade_date", desc=False)
            .limit(max(min_bars + 30, 400))
            .execute()
        )
        rows = list(getattr(res, "data", None) or [])
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return None, "table_missing"
        logger.info("%s read failed for %s: %s", TABLE, sym, exc)
        return None, "read_error"
    except Exception as exc:  # noqa: BLE001
        logger.info("%s read failed for %s: %s", TABLE, sym, exc)
        return None, "read_error"

    parsed = rows_to_ohlcv_dict(rows)
    err = _validate_cached(parsed, min_bars=min_bars, max_stale_trading_days=max_stale_trading_days)
    if err:
        return None, err
    _stats["supabase_hits"] += 1
    return parsed, None


def load_ohlcv_bulk_from_supabase(
    symbols: list[str],
    *,
    min_bars: int = _DEFAULT_MIN_BARS,
    max_stale_trading_days: int = _DEFAULT_MAX_STALE_TRADING_DAYS,
    client: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Prime in-memory cache for many symbols (best-effort)."""
    clear_bulk_cache()
    syms = sorted({str(s).strip().upper().replace(".NS", "") for s in symbols if s})
    if not syms:
        return {}

    sb = client if client is not None else _supabase_client()
    if sb is None:
        return {}

    chunk = 80
    for i in range(0, len(syms), chunk):
        batch = syms[i : i + chunk]
        try:
            res = (
                sb.table(TABLE)
                .select("symbol,trade_date,open,high,low,close,volume")
                .in_("symbol", batch)
                .order("trade_date", desc=False)
                .limit(max(len(batch) * 400, 500))
                .execute()
            )
            rows = list(getattr(res, "data", None) or [])
        except Exception as exc:  # noqa: BLE001
            logger.info("%s bulk read failed: %s", TABLE, exc)
            continue
        by_sym: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                by_sym.setdefault(sym, []).append(row)
        for sym, sym_rows in by_sym.items():
            parsed = rows_to_ohlcv_dict(sym_rows)
            if _validate_cached(parsed, min_bars=min_bars, max_stale_trading_days=max_stale_trading_days) is None:
                _BULK_CACHE[sym] = parsed
    return dict(_BULK_CACHE)


def query_max_trade_dates(client: Any, symbols: list[str]) -> dict[str, date | None]:
    """Return latest trade_date per symbol (per-symbol queries; used by ingest)."""
    out: dict[str, date | None] = {s: None for s in symbols}
    for sym in symbols:
        try:
            res = (
                client.table(TABLE)
                .select("trade_date")
                .eq("symbol", sym)
                .order("trade_date", desc=True)
                .limit(1)
                .execute()
            )
            rows = list(getattr(res, "data", None) or [])
            if rows and rows[0].get("trade_date"):
                out[sym] = date.fromisoformat(str(rows[0]["trade_date"])[:10])
        except Exception:  # noqa: BLE001
            continue
    return out
