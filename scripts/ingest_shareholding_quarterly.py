#!/usr/bin/env python3
"""Quarterly NSE shareholding / free-float ingest into Supabase.

Fetches the NSE ``corporate-share-holdings-master`` JSON feed (same cookie-warm-up
pattern as ``src/nse_eod.py`` / ``scripts/ingest_eod_feeds.py``) and upserts rows into
``shareholding_quarterly``.

``free_float_pct`` is mapped from NSE ``public_val`` (public shareholding %). When that
field is absent, ``100 - promoter_holding_pct`` is used as a fallback.

Usage:
  python scripts/ingest_shareholding_quarterly.py
  python scripts/ingest_shareholding_quarterly.py --symbol RELIANCE
  python scripts/ingest_shareholding_quarterly.py --start 2025-10-01 --end 2026-03-31
  python scripts/ingest_shareholding_quarterly.py --index sme

Set TITAN_NSE_INGEST_THROTTLE_SEC for a politeness delay between NSE calls.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import nse_eod
from config_loader import load_config
from supabase import create_client


def _upsert(client, table: str, rows: list[dict[str, Any]], on_conflict: str) -> tuple[int, str]:
    if not rows:
        return 0, "empty"
    try:
        for i in range(0, len(rows), 500):
            client.table(table).upsert(rows[i : i + 500], on_conflict=on_conflict).execute()
        return len(rows), "ok"
    except Exception as exc:  # noqa: BLE001
        return 0, f"error:{type(exc).__name__}:{exc}"


def _throttle_sec() -> float:
    raw = os.environ.get("TITAN_NSE_INGEST_THROTTLE_SEC", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        return 0.0


def _default_window() -> tuple[date, date]:
    """Last ~120 calendar days — captures the most recent quarterly filing window."""
    end = datetime.now().date()
    start = end - timedelta(days=120)
    return start, end


def ingest_shareholding(
    client,
    *,
    index: str = "equities",
    from_date: date | None = None,
    to_date: date | None = None,
    symbol: str | None = None,
) -> tuple[int, str]:
    if from_date is not None and to_date is not None:
        raw = nse_eod.fetch_shareholding_master(
            index=index, from_date=from_date, to_date=to_date, symbol=symbol
        )
    else:
        raw = nse_eod.fetch_shareholding_master(index=index, symbol=symbol)
    rows = nse_eod.normalize_shareholding_rows(raw)
    return _upsert(client, "shareholding_quarterly", rows, "symbol,as_of_date")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest NSE quarterly shareholding into Supabase")
    ap.add_argument("--symbol", type=str, default="", help="Optional single NSE symbol")
    ap.add_argument("--index", type=str, default="equities", choices=("equities", "sme"))
    ap.add_argument("--start", type=str, default="", help="Window start YYYY-MM-DD (with --end)")
    ap.add_argument("--end", type=str, default="", help="Window end YYYY-MM-DD (with --start)")
    ap.add_argument(
        "--latest-only",
        action="store_true",
        help="Skip date window; fetch NSE's most recent submissions only",
    )
    args = ap.parse_args()

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    symbol = args.symbol.strip().upper() or None
    from_date: date | None = None
    to_date: date | None = None
    if not args.latest_only:
        if args.start and args.end:
            from_date = date.fromisoformat(args.start)
            to_date = date.fromisoformat(args.end)
        elif not args.start and not args.end:
            from_date, to_date = _default_window()
        else:
            print("Provide both --start and --end, or omit both for the default window.", file=sys.stderr)
            return 1

    label = "latest"
    if from_date and to_date:
        label = f"{from_date.isoformat()}..{to_date.isoformat()}"
    sym_label = symbol or "all"
    print(f"Ingesting shareholding index={args.index} symbol={sym_label} window={label}")

    throttle = _throttle_sec()
    if throttle:
        time.sleep(throttle)

    n, status = ingest_shareholding(
        client,
        index=args.index,
        from_date=from_date,
        to_date=to_date,
        symbol=symbol,
    )
    print(f"  shareholding {sym_label} {label}: {n} rows ({status})")
    print("DONE.")
    return 0 if n or status == "empty" else 1


if __name__ == "__main__":
    raise SystemExit(main())
