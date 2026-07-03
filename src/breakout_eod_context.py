"""EOD context loaders for breakout scanner (Supabase + bhav cache)."""

from __future__ import annotations

import csv
import logging
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Supabase table for quarterly shareholding / free-float ingest.
_FREE_FLOAT_TABLE = "shareholding_quarterly"


def _supabase_client() -> Any | None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    return create_client(url, key)


def load_bhav_turnover_lacs_by_symbol(nse_cache_dir: Path) -> dict[str, float]:
    """Average daily turnover (lacs) per EQ symbol from cached bhav copies."""
    totals: dict[str, list[float]] = defaultdict(list)
    if not nse_cache_dir.is_dir():
        return {}
    for path in sorted(nse_cache_dir.glob("sec_bhavdata_full_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.reader(f):
                    if len(row) < 12:
                        continue
                    cells = [c.strip() for c in row]
                    if cells[1] != "EQ":
                        continue
                    sym = cells[0]
                    try:
                        turnover = float(cells[11])
                    except (TypeError, ValueError):
                        continue
                    if turnover > 0:
                        totals[sym].append(turnover)
        except OSError:
            continue
    return {sym: sum(vals) / len(vals) for sym, vals in totals.items() if vals}


def _avg_delivery_from_bhav_cache(nse_cache_dir: Path, symbols: set[str]) -> dict[str, float]:
    """Best-effort trailing average DELIV_PER from cached sec_bhavdata_full CSVs."""
    totals: dict[str, list[float]] = defaultdict(list)
    if not nse_cache_dir.is_dir() or not symbols:
        return {}
    for path in sorted(nse_cache_dir.glob("sec_bhavdata_full_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                cols = [c.strip().upper() for c in header]
                try:
                    sym_i = cols.index("SYMBOL")
                    ser_i = cols.index("SERIES")
                    deliv_i = cols.index("DELIV_PER")
                except ValueError:
                    continue
                for row in reader:
                    if len(row) <= max(sym_i, ser_i, deliv_i):
                        continue
                    if row[ser_i].strip() != "EQ":
                        continue
                    sym = row[sym_i].strip().upper()
                    if sym not in symbols:
                        continue
                    try:
                        val = float(row[deliv_i].strip())
                    except (TypeError, ValueError):
                        continue
                    if val >= 0:
                        totals[sym].append(val)
        except OSError:
            continue
    return {sym: sum(vals) / len(vals) for sym, vals in totals.items() if vals}


def load_delivery_pct_by_symbol(
    symbols: list[str],
    as_of_date: str | date | None = None,
    *,
    nse_cache_dir: Path | None = None,
) -> dict[str, float | None]:
    """
    Latest trailing average delivery % per symbol.

    Primary: Supabase ``delivery_daily.deliv_per`` (last 5 sessions mean).
    Fallback: cached bhav ``DELIV_PER`` when ``nse_cache_dir`` is provided.
    Missing symbols map to ``None``.
    """
    syms = sorted({str(s).strip().upper() for s in symbols if s})
    out: dict[str, float | None] = {sym: None for sym in syms}
    if not syms:
        return out

    as_of = as_of_date
    if isinstance(as_of, date):
        as_of = as_of.isoformat()
    if as_of is None:
        as_of = datetime.now(IST).date().isoformat()

    client = _supabase_client()
    if client is not None:
        try:
            res = (
                client.table("delivery_daily")
                .select("trade_date,symbol,deliv_per")
                .in_("symbol", syms)
                .lte("trade_date", as_of)
                .order("trade_date", desc=True)
                .limit(max(500, len(syms) * 10))
                .execute()
            )
            buckets: dict[str, list[float]] = defaultdict(list)
            for row in list(getattr(res, "data", None) or []):
                sym = str(row.get("symbol") or "").strip().upper()
                if sym not in out or len(buckets[sym]) >= 5:
                    continue
                try:
                    val = float(row.get("deliv_per"))
                except (TypeError, ValueError):
                    continue
                if val >= 0:
                    buckets[sym].append(val)
            for sym, vals in buckets.items():
                if vals:
                    out[sym] = round(sum(vals) / len(vals), 4)
        except APIError as exc:
            payload = exc.args[0] if exc.args else {}
            msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            if code != "PGRST205" and "could not find the table" not in msg.lower():
                logger.info("delivery_daily read failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.info("delivery_daily read failed: %s", exc)

    missing = {sym for sym, val in out.items() if val is None}
    if missing and nse_cache_dir is not None:
        bhav_avg = _avg_delivery_from_bhav_cache(nse_cache_dir, missing)
        for sym, val in bhav_avg.items():
            out[sym] = round(val, 4)

    return out


def load_free_float_pct_by_symbol(
    symbols: list[str],
    as_of_date: str | date | None = None,
) -> dict[str, float | None]:
    """
    Free-float % per symbol for liquidity quality scoring.

    Reads the latest ``shareholding_quarterly.free_float_pct`` row per symbol with
    ``as_of_date <=`` scan date. Populated by ``scripts/ingest_shareholding_quarterly.py``.
    Returns ``None`` when the table is empty or Supabase is unavailable.
    """
    syms = sorted({str(s).strip().upper() for s in symbols if s})
    out: dict[str, float | None] = {sym: None for sym in syms}
    if not syms:
        return out

    as_of = as_of_date
    if isinstance(as_of, date):
        as_of = as_of.isoformat()
    if as_of is None:
        as_of = datetime.now(IST).date().isoformat()

    client = _supabase_client()
    if client is None:
        return out

    try:
        res = (
            client.table(_FREE_FLOAT_TABLE)
            .select("symbol,as_of_date,free_float_pct")
            .in_("symbol", syms)
            .lte("as_of_date", as_of)
            .order("as_of_date", desc=True)
            .limit(max(500, len(syms) * 5))
            .execute()
        )
        for row in list(getattr(res, "data", None) or []):
            sym = str(row.get("symbol") or "").strip().upper()
            if sym not in out or out[sym] is not None:
                continue
            try:
                val = float(row.get("free_float_pct"))
            except (TypeError, ValueError):
                continue
            if val >= 0:
                out[sym] = round(val, 4)
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return out
        logger.info("%s read failed: %s", _FREE_FLOAT_TABLE, exc)
    except Exception as exc:  # noqa: BLE001
        logger.info("%s read failed: %s", _FREE_FLOAT_TABLE, exc)

    return out
