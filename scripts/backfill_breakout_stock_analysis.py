#!/usr/bin/env python3
"""Backfill historical breakout_stock_analysis rows via local replay.

Replays evaluate_bars_as_of (and PRE_BREAKOUT setup evaluation) over cached or
Supabase OHLCV, then upserts via persist_breakout_stock_analysis. Safe to re-run
with --skip-existing to avoid overwriting dates already present in Supabase.

Usage:
  python scripts/backfill_breakout_stock_analysis.py --start 2026-04-01 --end 2026-06-30 --dry-run
  python scripts/backfill_breakout_stock_analysis.py --start 2026-04-01 --end 2026-06-30 --skip-existing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
_scripts = str(Path(__file__).resolve().parent)
if _scripts in sys.path:
    sys.path.remove(_scripts)
sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / "config" / ".env")
load_dotenv(ROOT / ".env")

from breakout_backtest import (  # noqa: E402
    MIN_HISTORY_BARS,
    build_full_universe,
    fetch_universe_history,
    load_bhav_liquidity,
)
from breakout_breeze_codes import build_breeze_code_map  # noqa: E402
from breakout_eod_context import (  # noqa: E402
    load_delivery_pct_by_symbol,
    load_free_float_pct_by_symbol,
)
from breakout_scanner import (  # noqa: E402
    FILTERS,
    SIGNAL_TIER_PASS,
    SIGNAL_TIER_WATCH,
    _build_stock_analysis_record,
    evaluate_bars_as_of,
)
from breakout_sector_context import load_sector_lead_scores  # noqa: E402
from breakout_setup import evaluate_setup_as_of  # noqa: E402
from breakout_store import build_analysis_record, persist_breakout_stock_analysis  # noqa: E402
from config_loader import load_config  # noqa: E402
from postgrest.exceptions import APIError  # noqa: E402
from supabase import create_client  # noqa: E402

DEFAULT_MIN_SCAN = "2026-04-01"
RANGE_STR = "6m"
NSE_CACHE_DIR = ROOT / "temp" / "nse_cache"

# Columns always present on breakout_stock_analysis (pre-breeze/setup migrations).
_BASE_COLUMNS = set(build_analysis_record(
    run_id="x",
    scan_date="2026-01-01",
    ticker="X",
    tier="Small Cap",
    symbol_yahoo="X.NS",
).keys())

_OPTIONAL_COLUMNS = ("breeze_stock_code", "setup_trigger_price", "setup_rank")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill breakout_stock_analysis by replaying historical scans.",
    )
    p.add_argument(
        "--start",
        default=DEFAULT_MIN_SCAN,
        help=f"Inclusive first scan_date to backfill (default {DEFAULT_MIN_SCAN})",
    )
    p.add_argument(
        "--end",
        default="",
        help="Inclusive last scan_date (default: latest bar date in OHLCV cache)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Replay and summarize without writing to Supabase",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip scan_dates that already have rows in breakout_stock_analysis",
    )
    p.add_argument(
        "--no-prefer-supabase",
        action="store_true",
        help="Use Yahoo backtest cache only (do not load OHLCV from Supabase)",
    )
    return p.parse_args()


def _detect_allowed_columns(client) -> set[str]:
    """Probe live PostgREST schema; strip columns missing from the table."""
    allowed = set(_BASE_COLUMNS)
    for col in _OPTIONAL_COLUMNS:
        try:
            client.table("breakout_stock_analysis").select(col).limit(1).execute()
            allowed.add(col)
        except APIError:
            pass
    return allowed


def _filter_record(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k in allowed}


def _existing_scan_dates(client) -> set[str]:
    dates: set[str] = set()
    offset = 0
    while True:
        resp = (
            client.table("breakout_stock_analysis")
            .select("scan_date")
            .order("scan_date")
            .range(offset, offset + 999)
            .execute()
        )
        chunk = resp.data or []
        if not chunk:
            break
        for row in chunk:
            dates.add(str(row["scan_date"]))
        if len(chunk) < 1000:
            break
        offset += 1000
    return dates


def _count_rows(client) -> int:
    resp = (
        client.table("breakout_stock_analysis")
        .select("scan_date", count="exact")
        .limit(1)
        .execute()
    )
    return int(resp.count or 0)


def _enrich_universe_bhav_liquidity(universe: list[dict[str, Any]]) -> None:
    liquidity = load_bhav_liquidity(NSE_CACHE_DIR)
    for entry in universe:
        sym = entry["symbol"]
        if entry.get("liquidity_turnover_lacs_avg") is None and sym in liquidity:
            entry["liquidity_turnover_lacs_avg"] = round(liquidity[sym], 4)
            entry["liquidity_source"] = "bhav"


def _load_v7_context(
    symbols: list[str],
    scan_date: str,
) -> tuple[dict[str, float | None], dict[str, float], dict[str, float | None]]:
    delivery = load_delivery_pct_by_symbol(
        symbols,
        as_of_date=scan_date,
        nse_cache_dir=NSE_CACHE_DIR if NSE_CACHE_DIR.is_dir() else None,
    )
    sector_lead = load_sector_lead_scores(symbols, as_of_date=scan_date)
    free_float = load_free_float_pct_by_symbol(symbols, as_of_date=scan_date)
    return delivery, sector_lead, free_float


def _record_for_eval(
    *,
    run_id: str,
    scan_date: str,
    ticker: str,
    tier_key: str,
    df: dict[str, Any],
    eval_result: dict[str, Any],
    setup_result: dict[str, Any] | None,
    breeze_stock_code: str | None,
) -> dict[str, Any]:
    fail_reason = eval_result.get("fail_reason")
    signal_tier = eval_result.get("signal_tier")
    record_eval = eval_result if setup_result is None else {**eval_result, **setup_result}
    common = {
        "latest_price": eval_result["latest_price"],
        "prev_price": eval_result["prev_price"],
        "pct_change": eval_result["pct_change"],
        "vol_mult": eval_result["vol_mult"],
        "rsi_val": eval_result["rsi_val"],
        "adx_val": eval_result["adx_val"],
        "sma50_last": eval_result["sma50_last"],
        "sma_200_last": eval_result.get("sma_200_last"),
        "poc": eval_result["poc"],
        "vol_20_avg_last": eval_result["vol_20_avg_last"],
        "latest_volume": eval_result["latest_volume"],
        "sl_price": eval_result["sl_price"],
        "target_price": eval_result["target_price"],
        "target_gain": eval_result["target_gain"],
    }
    if fail_reason and setup_result is None:
        return _build_stock_analysis_record(
            run_id=run_id,
            scan_date=scan_date,
            ticker=ticker,
            tier_name=tier_key,
            df=df,
            fetch_err=None,
            fail_reason=fail_reason,
            passed=False,
            eval_result=record_eval,
            breeze_stock_code=breeze_stock_code,
            **common,
        )
    if setup_result is not None:
        return _build_stock_analysis_record(
            run_id=run_id,
            scan_date=scan_date,
            ticker=ticker,
            tier_name=tier_key,
            df=df,
            fetch_err=None,
            fail_reason=None,
            passed=False,
            eval_result=record_eval,
            breeze_stock_code=breeze_stock_code,
            **common,
        )
    return _build_stock_analysis_record(
        run_id=run_id,
        scan_date=scan_date,
        ticker=ticker,
        tier_name=tier_key,
        df=df,
        fetch_err=None,
        fail_reason=None,
        passed=bool(eval_result.get("passed")),
        eval_result=eval_result,
        breeze_stock_code=breeze_stock_code,
        **common,
    )


def main() -> int:
    args = _parse_args()
    t0 = time.monotonic()

    cfg = load_config(require_breeze=False, require_gemini=False)
    if not cfg.supabase_url or not cfg.supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY required", file=sys.stderr)
        return 1

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    allowed_columns = _detect_allowed_columns(client)
    missing_optional = [c for c in _OPTIONAL_COLUMNS if c not in allowed_columns]
    if missing_optional:
        print(
            f"Schema note: optional columns not yet on table (will strip): {missing_optional}. "
            "Run scripts/apply_breakout_stock_analysis_migration.py to add them.",
            flush=True,
        )
    else:
        print("Schema: breeze_stock_code and setup columns present.", flush=True)

    before_rows = _count_rows(client)
    existing_dates = _existing_scan_dates(client) if args.skip_existing else set()
    print(
        f"BEFORE rows={before_rows} existing_dates={len(existing_dates)} "
        f"range={min(existing_dates) if existing_dates else None}.."
        f"{max(existing_dates) if existing_dates else None}",
        flush=True,
    )

    universe_payload = build_full_universe()
    universe = universe_payload["stocks"]
    _enrich_universe_bhav_liquidity(universe)

    prefer_supabase = False if args.no_prefer_supabase else None
    data_by_symbol, fetch_manifest = fetch_universe_history(
        universe,
        range_str=RANGE_STR,
        warm_session=prefer_supabase is not True,
        prefer_supabase=prefer_supabase,
    )
    print(
        f"OHLCV loaded: {len(data_by_symbol)}/{len(universe)} "
        f"(prefer_supabase={fetch_manifest.get('prefer_supabase')})",
        flush=True,
    )
    ohlcv_stats = (fetch_manifest.get("ohlcv_stats") or {})
    if ohlcv_stats:
        print(
            f"  supabase_hits={ohlcv_stats.get('supabase_hits', 0)} "
            f"yahoo_fetches={ohlcv_stats.get('yahoo_fetches', 0)}",
            flush=True,
        )

    all_bar_dates: set[str] = set()
    for sd in data_by_symbol.values():
        all_bar_dates.update(sd["dates"])
    if not all_bar_dates:
        print("ERROR: no bar dates in loaded OHLCV", file=sys.stderr)
        return 1

    end_date = (args.end or "").strip() or max(all_bar_dates)
    start_date = (args.start or DEFAULT_MIN_SCAN).strip()
    candidate_dates = sorted(
        d for d in all_bar_dates if start_date <= d <= end_date
    )
    if args.skip_existing:
        target_dates = [d for d in candidate_dates if d not in existing_dates]
    else:
        target_dates = candidate_dates

    print(
        f"Window {start_date}..{end_date}: {len(candidate_dates)} bar dates, "
        f"{len(target_dates)} to backfill (skip_existing={args.skip_existing}, dry_run={args.dry_run})",
        flush=True,
    )
    if not target_dates:
        print("Nothing to backfill.")
        return 0

    target_set = set(target_dates)
    run_id_by_date = {d: f"backfill-{d}-{uuid.uuid4().hex[:8]}" for d in target_dates}
    records_by_date: dict[str, list[dict[str, Any]]] = {d: [] for d in target_dates}

    all_syms = [e["symbol"] for e in universe]
    turnover_by_sym = {e["symbol"]: e.get("liquidity_turnover_lacs_avg") for e in universe}
    breeze_map = build_breeze_code_map(all_syms, cfg)

    v7_dates = sorted(set(candidate_dates))
    print(f"Loading v7 context for {len(v7_dates)} trading dates...", flush=True)
    v7_by_date = {d: _load_v7_context(all_syms, d) for d in v7_dates}

    print("Replaying evaluations (single pass per symbol)...", flush=True)
    for i, entry in enumerate(universe):
        sym = entry["symbol"]
        sd = data_by_symbol.get(sym)
        if not sd:
            continue
        dates = sd["dates"]
        tier_key = sd["tier_key"]
        ticker = sd["yahoo_ticker"]
        df = sd["df"]
        breeze_code = breeze_map.get(sym)
        last_pass: int | None = None

        for idx in range(MIN_HISTORY_BARS - 1, len(dates)):
            d = dates[idx]
            delivery, sector_lead_map, free_float = v7_by_date.get(d, ({}, {}, {}))
            eval_result = evaluate_bars_as_of(
                df,
                idx,
                tier_key,
                last_pass_idx=last_pass,
                bhav_turnover_lacs=turnover_by_sym.get(sym),
                delivery_pct=delivery.get(sym),
                free_float_pct=free_float.get(sym),
                sector_lead=sector_lead_map.get(sym),
            )
            if eval_result.get("passed"):
                last_pass = idx

            if d not in target_set:
                continue

            setup_result = None
            fail_reason = eval_result.get("fail_reason")
            signal_tier = eval_result.get("signal_tier")
            if fail_reason and signal_tier not in (SIGNAL_TIER_PASS, SIGNAL_TIER_WATCH):
                filt = FILTERS[tier_key]
                setup_result = evaluate_setup_as_of(
                    df,
                    idx,
                    tier_key,
                    min_price=filt["min_price"],
                    vol_mult_threshold=filt["vol_mult"],
                    bhav_turnover_lacs=turnover_by_sym.get(sym),
                    delivery_pct=delivery.get(sym),
                    free_float_pct=free_float.get(sym),
                    sector_lead=sector_lead_map.get(sym),
                    rsi_val=eval_result.get("rsi_val"),
                    adx_val=eval_result.get("adx_val"),
                    pct_change=eval_result.get("pct_change"),
                    vol_mult=eval_result.get("vol_mult"),
                    sma20_last=eval_result.get("sma20_last"),
                    sma50_last=eval_result.get("sma50_last"),
                )

            records_by_date[d].append(
                _filter_record(
                    _record_for_eval(
                        run_id=run_id_by_date[d],
                        scan_date=d,
                        ticker=ticker,
                        tier_key=tier_key,
                        df=df,
                        eval_result=eval_result,
                        setup_result=setup_result,
                        breeze_stock_code=breeze_code,
                    ),
                    allowed_columns,
                )
            )

        if (i + 1) % 50 == 0:
            print(f"  symbols processed: {i + 1}/{len(universe)}", flush=True)

    total_records = sum(len(v) for v in records_by_date.values())
    pass_by_date = {
        d: sum(1 for r in records_by_date[d] if r.get("passed")) for d in target_dates
    }
    print(
        f"Replay complete: {total_records} records across {len(target_dates)} dates "
        f"(avg {total_records // max(len(target_dates), 1)} rows/day)",
        flush=True,
    )

    if args.dry_run:
        sample = target_dates[:3]
        summary = {
            "dry_run": True,
            "dates": len(target_dates),
            "total_records": total_records,
            "pass_counts_sample": {d: pass_by_date[d] for d in sample},
            "first_date": target_dates[0],
            "last_date": target_dates[-1],
            "allowed_columns": sorted(allowed_columns),
            "stripped_optional": missing_optional,
        }
        print(json.dumps(summary, indent=2))
        print(f"Dry-run complete (elapsed_sec={round(time.monotonic() - t0, 1)})")
        return 0

    total_persisted = 0
    days_done = 0
    failures: list[str] = []

    for scan_date in target_dates:
        records = records_by_date[scan_date]
        meta = persist_breakout_stock_analysis(cfg, records)
        if not meta.get("persisted"):
            failures.append(f"{scan_date}: {meta}")
        else:
            total_persisted += int(meta.get("rows") or 0)
            days_done += 1
            print(
                f"  persisted {scan_date}: rows={meta.get('rows')} "
                f"pass={pass_by_date[scan_date]} run_id={run_id_by_date[scan_date]}",
                flush=True,
            )

    after_rows = _count_rows(client)
    after_dates = _existing_scan_dates(client)
    elapsed = round(time.monotonic() - t0, 1)
    print("--- SUMMARY ---")
    print(f"elapsed_sec={elapsed}")
    print(f"days_backfilled={days_done}/{len(target_dates)}")
    print(f"rows_persisted={total_persisted}")
    print(f"BEFORE rows={before_rows}")
    print(f"AFTER rows={after_rows} dates={len(after_dates)}")
    if after_dates:
        print(f"date_range={min(after_dates)}..{max(after_dates)}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures[:10]:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
