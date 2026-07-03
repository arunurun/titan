#!/usr/bin/env python3
"""Incremental Yahoo OHLCV ingest for breakout universe into Supabase ``equity_ohlcv_daily``.

Loads Nifty Smallcap 100 + Microcap 250 symbols (same NSE index CSVs as ``breakout_scanner``),
fetches missing or stale bars from Yahoo, and upserts into Supabase.

Usage:
  python scripts/ingest_breakout_ohlcv.py
  python scripts/ingest_breakout_ohlcv.py --symbol RELIANCE
  python scripts/ingest_breakout_ohlcv.py --full-backfill
  python scripts/ingest_breakout_ohlcv.py --dry-run --workers 4

Set TITAN_YAHOO_INGEST_THROTTLE_SEC for a politeness delay between Yahoo calls.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from breakout_ohlcv_store import (  # noqa: E402
    TABLE,
    ohlcv_dict_to_rows,
    query_max_trade_dates,
)
from breakout_scanner import (  # noqa: E402
    INDEX_URLS,
    download_nse_tickers,
    fetch_yahoo_data,
    warm_yahoo_session,
    _resolve_yahoo_ticker,
)
from config_loader import load_config  # noqa: E402
from supabase import create_client  # noqa: E402


def _throttle_sec() -> float:
    raw = os.environ.get("TITAN_YAHOO_INGEST_THROTTLE_SEC", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.35
    except ValueError:
        return 0.35


def _upsert(client, rows: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, str]:
    if not rows:
        return 0, "empty"
    if dry_run:
        return len(rows), "dry_run"
    try:
        for i in range(0, len(rows), 500):
            client.table(TABLE).upsert(rows[i : i + 500], on_conflict="symbol,trade_date").execute()
        return len(rows), "ok"
    except Exception as exc:  # noqa: BLE001
        return 0, f"error:{type(exc).__name__}:{exc}"


def load_universe_symbols(*, symbol: str | None = None) -> list[tuple[str, str]]:
    """Return (NSE symbol, Yahoo ticker) pairs for the breakout universes."""
    if symbol:
        sym = symbol.strip().upper()
        return [(sym, _resolve_yahoo_ticker(f"{sym}.NS"))]

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in INDEX_URLS.values():
        for ticker in download_nse_tickers(url):
            sym = ticker.replace(".NS", "").upper()
            if sym in seen:
                continue
            seen.add(sym)
            pairs.append((sym, _resolve_yahoo_ticker(ticker)))
    return pairs


def _filter_new_rows(rows: list[dict[str, Any]], since: date | None) -> list[dict[str, Any]]:
    if since is None:
        return rows
    return [r for r in rows if date.fromisoformat(str(r["trade_date"])[:10]) > since]


def ingest_symbol(
    client,
    sym: str,
    yahoo_ticker: str,
    *,
    max_date: date | None,
    full_backfill: bool,
    dry_run: bool,
    throttle: float,
) -> tuple[str, int, str]:
    if throttle > 0:
        time.sleep(throttle)

    if full_backfill or max_date is None:
        parsed, err = fetch_yahoo_data(yahoo_ticker, skip_supabase=True)
        since = None
    else:
        parsed, err = fetch_yahoo_data(yahoo_ticker, skip_supabase=True)
        since = max_date

    if not parsed or err:
        return sym, 0, err or "fetch_failed"

    all_rows = ohlcv_dict_to_rows(sym, parsed)
    rows = all_rows if full_backfill or max_date is None else _filter_new_rows(all_rows, since)
    n, status = _upsert(client, rows, dry_run=dry_run)
    return sym, n, status


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest breakout universe OHLCV into Supabase")
    ap.add_argument("--symbol", type=str, default="", help="Optional single NSE symbol")
    ap.add_argument("--full-backfill", action="store_true", help="Upsert full ~1y history per symbol")
    ap.add_argument("--dry-run", action="store_true", help="Fetch and parse only; no Supabase writes")
    ap.add_argument("--workers", type=int, default=4, help="Parallel Yahoo workers (default 4)")
    args = ap.parse_args()

    workers = max(1, min(6, int(args.workers)))
    symbol = args.symbol.strip().upper() or None
    universe = load_universe_symbols(symbol=symbol)
    if not universe:
        print("No symbols to ingest.", file=sys.stderr)
        return 1

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    syms = [s for s, _ in universe]
    max_dates = query_max_trade_dates(client, syms) if not args.full_backfill else {s: None for s in syms}

    print(f"Ingesting {len(universe)} symbols workers={workers} full_backfill={args.full_backfill} dry_run={args.dry_run}")
    warm_yahoo_session()

    throttle = _throttle_sec()
    total_rows = 0
    ok = 0
    failed = 0

    def _task(item: tuple[str, str]) -> tuple[str, int, str]:
        sym, ticker = item
        return ingest_symbol(
            client,
            sym,
            ticker,
            max_date=max_dates.get(sym),
            full_backfill=args.full_backfill,
            dry_run=args.dry_run,
            throttle=throttle,
        )

    if workers == 1:
        results = [_task(item) for item in universe]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, item): item for item in universe}
            for fut in as_completed(futures):
                results.append(fut.result())

    for sym, n, status in sorted(results, key=lambda x: x[0]):
        total_rows += n
        if n > 0 or status in ("empty", "dry_run"):
            ok += 1
            print(f"  {sym}: {n} rows ({status})")
        else:
            failed += 1
            print(f"  {sym}: FAILED ({status})")

    print(f"DONE. symbols_ok={ok} failed={failed} rows={total_rows}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
