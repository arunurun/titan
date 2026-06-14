#!/usr/bin/env python3
"""Backfill v2 risk-gate inputs (P0-3) + session move (P1-5) into tape_extras.

Recomputes money-flow / ADX-regime / over-extension inputs from FREE NSE EOD OHLCV
(sec_bhavdata_full, whole-market) and merges them into existing
``symbol_daily_features.tape_extras`` rows. No Breeze, no DDL: it only updates the
existing jsonb column, on the natural key (trade_date, sector, symbol, exchange).

Idempotent: re-running recomputes identical values and merges (never drops existing
tape_extras keys). Resilient: per-symbol failures are recorded and skipped, never crash.

Keys written into tape_extras:
  cmf_20, obv_slope_20, adx_14, adx_plus_di_14, adx_minus_di_14, ema200_stretch_atr,
  sector_pctile_ema200_stretch, sector_pctile_cmf_20, sector_pctile_adx_14,
  session_move_vs_prev_close_pct (only when missing/None).

Usage:
  python scripts/backfill_v2_inputs.py                 # all rows in the table
  python scripts/backfill_v2_inputs.py --days 60       # last 60 trade dates
  python scripts/backfill_v2_inputs.py --symbols DIXON,CANBK,PNB
  python scripts/backfill_v2_inputs.py --sectors banks_psu,auto --dry-run
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
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
from tape_metrics import percentile_rank_0_100
from titan_engine import (
    calculate_adx,
    calculate_cmf,
    calculate_latest_di,
    calculate_obv_slope,
)

LOOKBACK_CAL_DAYS = 50  # trailing calendar days for the 20-28 session indicators


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _round_or_none(x: float, n: int = 4) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, n)


def fetch_feature_rows(client, *, days: int | None, symbols: list[str] | None, sectors: list[str] | None):
    cols = "trade_date,sector,symbol,exchange,ema_200_distance_pct,atr_14_pct,tape_extras"
    start = None
    if days:
        mx = client.table("symbol_daily_features").select("trade_date").order("trade_date", desc=True).limit(1).execute().data
        if mx:
            max_d = date.fromisoformat(mx[0]["trade_date"])
            # approximate: days trade-sessions ~ days*1.5 calendar days
            start = (max_d - timedelta(days=int(days * 1.6) + 5)).isoformat()
    rows: list[dict[str, Any]] = []
    page = 1000
    offset = 0
    while True:
        q = client.table("symbol_daily_features").select(cols).order("trade_date").range(offset, offset + page - 1)
        if start:
            q = q.gte("trade_date", start)
        if symbols:
            q = q.in_("symbol", [s.strip().upper() for s in symbols])
        if sectors:
            q = q.in_("sector", [s.strip().lower() for s in sectors])
        batch = q.execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="limit to last N trade dates (default: all)")
    ap.add_argument("--symbols", type=str, default="", help="comma-separated symbol filter")
    ap.add_argument("--sectors", type=str, default="", help="comma-separated sector filter")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    symbols = [s for s in args.symbols.split(",") if s.strip()] or None
    sectors = [s for s in args.sectors.split(",") if s.strip()] or None

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    rows = fetch_feature_rows(client, days=args.days, symbols=symbols, sectors=sectors)
    if not rows:
        print("No feature rows matched the filter.")
        return 0

    feat_dates = sorted({date.fromisoformat(r["trade_date"]) for r in rows})
    want_symbols = {str(r["symbol"]).strip().upper() for r in rows}
    panel_start = feat_dates[0] - timedelta(days=LOOKBACK_CAL_DAYS)
    panel_end = feat_dates[-1]
    print(
        f"Feature rows={len(rows)} | symbols={len(want_symbols)} | "
        f"trade_dates={len(feat_dates)} ({feat_dates[0]}..{feat_dates[-1]})"
    )
    print(f"Building OHLCV panel {panel_start}..{panel_end} (whole-market bhavcopy, cached)...")
    panel = nse_eod.build_ohlcv_panel(panel_start, panel_end, symbols=want_symbols)
    print(f"Panel symbols available: {len(panel)} / {len(want_symbols)} requested")

    # Compute per-row indicators first (so sector percentiles can use the cross-section).
    computed: dict[int, dict[str, Any]] = {}
    missing_symbols: set[str] = set()
    for idx, r in enumerate(rows):
        sym = str(r["symbol"]).strip().upper()
        td = date.fromisoformat(r["trade_date"])
        df = panel.get(sym)
        vals: dict[str, Any] = {}
        # ema200_stretch_atr from stored columns (cheap, matches backtest derivation).
        ema_dist = _f(r.get("ema_200_distance_pct"))
        atr_pct = _f(r.get("atr_14_pct"))
        if not math.isnan(ema_dist) and not math.isnan(atr_pct) and atr_pct != 0.0:
            vals["ema200_stretch_atr"] = round(ema_dist / atr_pct, 4)
        if df is not None and not df.empty:
            hist = df[df["trade_date"] <= td]
            if len(hist) >= 5:
                vals["cmf_20"] = _round_or_none(calculate_cmf(hist, 20))
                vals["obv_slope_20"] = _round_or_none(calculate_obv_slope(hist, 20), 2)
                vals["adx_14"] = _round_or_none(calculate_adx(hist, 14), 2)
                pdi, mdi = calculate_latest_di(hist, 14)
                vals["adx_plus_di_14"] = _round_or_none(pdi, 2)
                vals["adx_minus_di_14"] = _round_or_none(mdi, 2)
                # session move from bhavcopy close vs prev_close (P1-5), only when missing.
                trow = hist[hist["trade_date"] == td]
                if not trow.empty:
                    close = _f(trow["close"].iloc[0])
                    prev_close = _f(trow["prev_close"].iloc[0])
                    if not math.isnan(close) and not math.isnan(prev_close) and prev_close != 0.0:
                        vals["session_move_vs_prev_close_pct"] = round((close / prev_close - 1.0) * 100.0, 4)
        else:
            missing_symbols.add(sym)
        computed[idx] = vals

    # Sector cross-section percentiles per (sector, trade_date).
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        groups[(str(r.get("sector") or "").strip().lower(), r["trade_date"])].append(idx)
    for (_sec, _td), idxs in groups.items():
        for src_key, dst_key in (
            ("ema200_stretch_atr", "sector_pctile_ema200_stretch"),
            ("cmf_20", "sector_pctile_cmf_20"),
            ("adx_14", "sector_pctile_adx_14"),
        ):
            series = [computed[i].get(src_key) for i in idxs]
            series = [float(v) for v in series if v is not None]
            for i in idxs:
                v = computed[i].get(src_key)
                if v is not None and series:
                    computed[i][dst_key] = percentile_rank_0_100(series, float(v))

    # Merge into tape_extras and upsert in batches.
    payloads: list[dict[str, Any]] = []
    touched = 0
    for idx, r in enumerate(rows):
        new_vals = computed.get(idx) or {}
        if not new_vals:
            continue
        tape = r.get("tape_extras")
        tape = dict(tape) if isinstance(tape, dict) else {}
        changed = False
        for k, v in new_vals.items():
            # session move only when missing; everything else is authoritative recompute.
            if k == "session_move_vs_prev_close_pct" and tape.get(k) is not None:
                continue
            if tape.get(k) != v:
                tape[k] = v
                changed = True
        if not changed:
            continue
        touched += 1
        payloads.append(
            {
                "trade_date": r["trade_date"],
                "sector": r["sector"],
                "symbol": r["symbol"],
                "exchange": r["exchange"],
                "tape_extras": tape,
            }
        )

    print(f"Rows with new values: {touched} | symbols missing from bhavcopy: {sorted(missing_symbols)[:20]}"
          f"{' ...' if len(missing_symbols) > 20 else ''} ({len(missing_symbols)})")
    if args.dry_run:
        print("DRY RUN — no writes. Sample:")
        for p in payloads[:3]:
            print("  ", p["trade_date"], p["symbol"], {k: p["tape_extras"].get(k) for k in
                  ("cmf_20", "adx_14", "obv_slope_20", "ema200_stretch_atr", "sector_pctile_ema200_stretch")})
        return 0

    # UPDATE in place by natural key (never inserts -> never trips NOT NULL columns).
    written = 0
    failed = 0
    for p in payloads:
        try:
            (
                client.table("symbol_daily_features")
                .update({"tape_extras": p["tape_extras"]})
                .eq("trade_date", p["trade_date"])
                .eq("sector", p["sector"])
                .eq("symbol", p["symbol"])
                .eq("exchange", p["exchange"])
                .execute()
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - resilient backfill
            failed += 1
            if failed <= 5:
                print(f"  update failed {p['symbol']}/{p['trade_date']}: {exc}")
        if written % 500 == 0 and written:
            print(f"  updated {written}/{len(payloads)}")
    print(f"DONE: updated tape_extras on {written} rows ({failed} failures).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
