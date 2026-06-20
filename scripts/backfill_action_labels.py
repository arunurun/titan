#!/usr/bin/env python3
"""Backfill stale ``symbol_daily_features.action_signal`` rows from stored inputs.

Recomputes labels with the current always-on ``evaluate_signal_v2`` path (via
``signal_v2_backtest.recompute_label``) without Breeze or live market fetch.
Safe to re-run: only rows where stored != recomputed are patched.

Usage:
  python scripts/backfill_action_labels.py --start 2026-05-01 --end 2026-06-20 --dry-run
  python scripts/backfill_action_labels.py --start 2026-05-01 --end 2026-06-20
  TITAN_RECONCILE_MODE=1 python scripts/backfill_action_labels.py --days 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from analysis_store import analysis_store_enabled, persist_action_label_backfill
from config_loader import load_config


def _resolve_window(args: argparse.Namespace) -> tuple[str, str]:
    if args.start and args.end:
        return args.start.strip(), args.end.strip()
    cfg = load_config(require_breeze=False, require_gemini=False)
    from supabase import create_client

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    mx = (
        client.table("symbol_daily_features")
        .select("trade_date")
        .order("trade_date", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not mx:
        raise SystemExit("No symbol_daily_features rows; cannot infer end date.")
    end = date.fromisoformat(str(mx[0]["trade_date"]))
    days = max(1, int(args.days or 60))
    start = end - timedelta(days=int(days * 1.6) + 5)
    return start.isoformat(), end.isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill stale action_signal labels in Supabase.")
    ap.add_argument("--start", type=str, default="", help="Inclusive start trade_date (YYYY-MM-DD)")
    ap.add_argument("--end", type=str, default="", help="Inclusive end trade_date (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=60, help="When --start/--end omitted, backfill last N trade dates")
    ap.add_argument("--sector", type=str, default="", help="Optional single-sector filter")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("TITAN_ENABLE_ANALYSIS_STORE", "1")
    os.environ.setdefault("TITAN_RECONCILE_MODE", "1")

    if not analysis_store_enabled():
        print("ERROR: TITAN_ENABLE_ANALYSIS_STORE=1 and Supabase creds required.", file=sys.stderr)
        return 1

    start_iso, end_iso = _resolve_window(args)
    sector = args.sector.strip().lower() or None
    all_stocks = sector is None

    cfg = load_config(require_breeze=False, require_gemini=False)
    if not cfg.supabase_url or not cfg.supabase_key:
        print("ERROR: SUPABASE_URL / SUPABASE_KEY missing.", file=sys.stderr)
        return 1

    print(
        f"[backfill] reconcile_mode={os.environ.get('TITAN_RECONCILE_MODE')} "
        f"scope={'all-stocks' if all_stocks else sector} window={start_iso}..{end_iso} "
        f"dry_run={args.dry_run}"
    )
    out = persist_action_label_backfill(
        cfg,
        start_date=start_iso,
        end_date=end_iso,
        sector=sector,
        all_stocks=all_stocks,
        dry_run=args.dry_run,
    )
    print(json.dumps(out, indent=2, default=str))
    if out.get("reason") == "analysis_store_disabled":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
