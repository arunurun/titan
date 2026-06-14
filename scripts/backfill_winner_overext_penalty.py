#!/usr/bin/env python3
"""P0-1 (data side): backfill the null overextension_penalty on the existing
sector_priority_rankings + sector_daily_winners rows WITHOUT a Breeze session.

The live ranking pipeline (build_sector_rankings) needs Breeze, so refreshing as_of_date
to today is Breeze-BLOCKED. However the penalty itself only needs return_1w/1m_pct +
absorption_ratio (already stored on each ranking row) and ATR-normalized EMA200 stretch
(now stored in symbol_daily_features.tape_extras.ema200_stretch_atr by the P0-3 backfill).
This recomputes the penalty with src.sector_priority._overextension_penalty and writes it
additively into ranking.meta and winners.score_breakdown (no reordering, no row deletes).

Usage:
  python scripts/backfill_winner_overext_penalty.py --dry-run
  python scripts/backfill_winner_overext_penalty.py
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from sector_priority import _overextension_penalty
from supabase import create_client


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    # 1) Load all ranking rows (paginate).
    rankings: list[dict[str, Any]] = []
    off = 0
    while True:
        b = (client.table("sector_priority_rankings")
             .select("id,sector_key,as_of_date,symbol,exchange,return_1w_pct,return_1m_pct,absorption_ratio,meta")
             .order("as_of_date").range(off, off + 999).execute().data or [])
        rankings.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    print(f"Loaded {len(rankings)} ranking rows")
    if not rankings:
        print("No ranking rows; nothing to do."); return 0

    # 2) Stretch lookup per (symbol, as_of): latest feature row <= as_of within a 12d window.
    symbols = sorted({str(r["symbol"]).strip().upper() for r in rankings})
    as_ofs = sorted({str(r["as_of_date"]) for r in rankings})
    min_d = (date.fromisoformat(min(as_ofs)) - timedelta(days=12)).isoformat()
    max_d = max(as_ofs)
    feats: dict[str, list[dict[str, Any]]] = {}
    off = 0
    while True:
        b = (client.table("symbol_daily_features")
             .select("symbol,trade_date,ema_200_distance_pct,atr_14_pct,tape_extras")
             .gte("trade_date", min_d).lte("trade_date", max_d)
             .in_("symbol", symbols).order("trade_date").range(off, off + 999).execute().data or [])
        for r in b:
            feats.setdefault(str(r["symbol"]).strip().upper(), []).append(r)
        if len(b) < 1000:
            break
        off += 1000

    def stretch_for(sym: str, as_of: str) -> tuple[float, float]:
        rows = [r for r in feats.get(sym, []) if str(r["trade_date"]) <= as_of]
        if not rows:
            return float("nan"), float("nan")
        r = rows[-1]
        te = r.get("tape_extras") or {}
        stretch = _f(te.get("ema200_stretch_atr"))
        if math.isnan(stretch):
            ed, ap = _f(r.get("ema_200_distance_pct")), _f(r.get("atr_14_pct"))
            if not math.isnan(ed) and not math.isnan(ap) and ap != 0.0:
                stretch = ed / ap
        return stretch, _f(r.get("ema_200_distance_pct"))

    # 3) Recompute penalty + update ranking.meta.
    rank_updates = 0
    winner_payload: dict[tuple[str, str, str], dict[str, Any]] = {}
    samples = []
    for r in rankings:
        sym = str(r["symbol"]).strip().upper()
        as_of = str(r["as_of_date"])
        stretch, ema_dist = stretch_for(sym, as_of)
        pen = _overextension_penalty(
            ret_1w=_f(r.get("return_1w_pct")), ret_1m=_f(r.get("return_1m_pct")),
            absorption=_f(r.get("absorption_ratio")), stretch=stretch, ema_dist=ema_dist,
        )
        meta = dict(r.get("meta") or {})
        meta["overextension_penalty"] = pen["penalty"]
        meta["overextension_components"] = pen["components"]
        winner_payload[(r["sector_key"], as_of, sym)] = pen
        if len(samples) < 10:
            samples.append((sym, as_of, round(stretch, 2) if not math.isnan(stretch) else None,
                            r.get("return_1w_pct"), pen["penalty"]))
        if not args.dry_run:
            client.table("sector_priority_rankings").update({"meta": meta}).eq("id", r["id"]).execute()
        rank_updates += 1
        if rank_updates % 200 == 0 and not args.dry_run:
            print(f"  rankings updated {rank_updates}...")

    # 4) Update sector_daily_winners.score_breakdown for matching rows.
    winners: list[dict[str, Any]] = []
    off = 0
    while True:
        b = (client.table("sector_daily_winners")
             .select("sector_key,as_of_date,winner_rank,symbol,score_breakdown")
             .order("as_of_date").range(off, off + 999).execute().data or [])
        winners.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    win_updates = 0
    for w in winners:
        key = (w["sector_key"], str(w["as_of_date"]), str(w["symbol"]).strip().upper())
        pen = winner_payload.get(key)
        if not pen:
            continue
        sb = dict(w.get("score_breakdown") or {})
        if sb.get("overextension_penalty") == pen["penalty"]:
            continue
        sb["overextension_penalty"] = pen["penalty"]
        sb["overextension_components"] = pen["components"]
        if not args.dry_run:
            (client.table("sector_daily_winners").update({"score_breakdown": sb})
             .eq("sector_key", w["sector_key"]).eq("as_of_date", w["as_of_date"])
             .eq("winner_rank", w["winner_rank"]).execute())
        win_updates += 1

    print(f"\nSamples (symbol, as_of, stretch, ret_1w%, penalty):")
    for s in samples:
        print("  ", s)
    print(f"\nrankings to update: {rank_updates} | winners to update: {win_updates}")
    print("DRY RUN — no writes." if args.dry_run else "DONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
