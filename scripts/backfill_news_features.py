#!/usr/bin/env python3
"""Backfill P1-2: populate news_count / news_sentiment_score / news_sentiment_aggregate /
news_sentiment_trend on symbol_daily_features from the latest symbol_news_snapshots
per symbol as-of each feature trade_date.

Root cause being fixed: per-symbol snapshots are refreshed by a job that runs AFTER the
sector feature run, so the feature rows were written with news_count=0 in ~87-97% of
recent rows. This joins the most recent snapshot (snapshot_at <= trade_date EOD) onto
each row. In-place UPDATE on the natural key (no DDL, idempotent).

Usage:
  python scripts/backfill_news_features.py                 # all rows
  python scripts/backfill_news_features.py --symbols DIXON,CANBK --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from supabase import create_client


def _snap_date(value: Any) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    # 1) Load all snapshots, group by symbol sorted by snapshot date ascending.
    snaps: dict[str, list[dict[str, Any]]] = {}
    off = 0
    while True:
        b = (client.table("symbol_news_snapshots")
             .select("symbol,snapshot_at,news_count,aggregate_score,aggregate_sentiment,sentiment_trend")
             .order("snapshot_at").range(off, off + 999).execute().data or [])
        for r in b:
            sym = str(r.get("symbol") or "").strip().upper()
            d = _snap_date(r.get("snapshot_at"))
            if sym and d:
                r["_d"] = d
                snaps.setdefault(sym, []).append(r)
        if len(b) < 1000:
            break
        off += 1000
    print(f"Loaded snapshots for {len(snaps)} symbols")

    # 2) Load feature rows.
    cols = "trade_date,sector,symbol,exchange,news_count,news_sentiment_score"
    rows: list[dict[str, Any]] = []
    off = 0
    while True:
        q = client.table("symbol_daily_features").select(cols).order("trade_date").range(off, off + 999)
        if symbols:
            q = q.in_("symbol", symbols)
        b = q.execute().data or []
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    print(f"Loaded {len(rows)} feature rows")

    def latest_snap_asof(sym: str, td: date) -> dict[str, Any] | None:
        cands = snaps.get(sym) or []
        chosen = None
        for s in cands:
            if s["_d"] <= td and int(s.get("news_count") or 0) > 0:
                chosen = s  # ascending order -> last match is latest <= td
        return chosen

    updates = 0
    matched = 0
    samples = []
    for r in rows:
        sym = str(r["symbol"]).strip().upper()
        td = date.fromisoformat(r["trade_date"])
        snap = latest_snap_asof(sym, td)
        if not snap:
            continue
        matched += 1
        new_count = int(snap.get("news_count") or 0)
        new_score = float(snap.get("aggregate_score") or 0.0)
        new_agg = str(snap.get("aggregate_sentiment") or "neutral")
        trend = snap.get("sentiment_trend")
        new_trend = str(trend) if trend is not None else "n/a"
        # Only write when it would change the row (idempotent).
        if (int(r.get("news_count") or 0) == new_count
                and float(r.get("news_sentiment_score") or 0.0) == new_score):
            continue
        updates += 1
        if len(samples) < 8:
            samples.append((sym, r["trade_date"], new_count, new_score, new_agg))
        if not args.dry_run:
            (client.table("symbol_daily_features")
             .update({
                 "news_count": new_count,
                 "news_sentiment_score": new_score,
                 "news_sentiment_aggregate": new_agg,
                 "news_sentiment_trend": new_trend,
             })
             .eq("trade_date", r["trade_date"]).eq("sector", r["sector"])
             .eq("symbol", r["symbol"]).eq("exchange", r["exchange"]).execute())
        if updates % 500 == 0 and not args.dry_run:
            print(f"  updated {updates}...")

    print(f"Rows matched to a snapshot: {matched} | rows to update: {updates}")
    print("Samples (symbol, date, news_count, score, sentiment):")
    for s in samples:
        print("  ", s)
    if args.dry_run:
        print("DRY RUN — no writes.")
    else:
        print(f"DONE: updated {updates} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
