#!/usr/bin/env python3
"""Breakout old (Supabase) vs new (local replay) efficacy reconciliation (~3 months).

Compares persisted Supabase breakout_stock_analysis runs against a full-universe
replay via evaluate_bars_as_of (current main logic), including forward validation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
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
    FORWARD_HORIZONS,
    MIN_HISTORY_BARS,
    _aggregate_hit_rates,
    build_full_universe,
    fetch_universe_history,
    fetch_universe_history_throttled,
    load_bhav_liquidity,
    load_cached_universe_history,
    validate_forward_path,
)
from breakout_eod_context import (  # noqa: E402
    load_delivery_pct_by_symbol,
    load_free_float_pct_by_symbol,
)
from breakout_sector_context import load_sector_lead_scores  # noqa: E402
from breakout_scanner import (  # noqa: E402
    FILTERS,
    SIGNAL_TIER_WATCH,
    bar_dates_from_df,
    evaluate_bars_as_of,
    fetch_yahoo_history,
    _yahoo_backtest_cache_path,
)
from supabase import create_client  # noqa: E402

OUT_DIR = ROOT / "output" / "breakoutcheck"
DEFAULT_MIN_SCAN = "2026-04-01"
TRADING_DAYS_TARGET = 66
RANGE_STR = "6m"
NSE_CACHE_DIR = ROOT / "temp" / "nse_cache"
_TIER_LABEL_TO_KEY = {v["type"]: k for k, v in FILTERS.items()}
GHA_RUN_ID = "28726866835"


def _git_branch() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _tier_key_from_label(tier_label: str) -> str:
    if tier_label in _TIER_LABEL_TO_KEY:
        return _TIER_LABEL_TO_KEY[tier_label]
    if "micro" in tier_label.lower():
        return "MICRO_CAP_250"
    return "SMALL_CAP_100"


def _enrich_universe_bhav_liquidity(universe: list[dict[str, Any]]) -> None:
    liquidity = load_bhav_liquidity(NSE_CACHE_DIR)
    for entry in universe:
        sym = entry["symbol"]
        if entry.get("liquidity_turnover_lacs_avg") is None and sym in liquidity:
            entry["liquidity_turnover_lacs_avg"] = round(liquidity[sym], 4)
            entry["liquidity_source"] = "bhav"


def _fetch_missing_cache(
    universe: list[dict[str, Any]],
    data_by_symbol: dict[str, dict[str, Any]],
    *,
    range_str: str = RANGE_STR,
) -> dict[str, Any]:
    missing = [e for e in universe if e["symbol"] not in data_by_symbol]
    if not missing:
        return {"missing": 0, "fetched": 0, "failed": 0}
    fetched, manifest = fetch_universe_history_throttled(missing, range_str=range_str)
    for sym, sd in fetched.items():
        entry = next((e for e in universe if e["symbol"] == sym), None)
        if entry:
            sd["liquidity_turnover_lacs_avg"] = entry.get("liquidity_turnover_lacs_avg")
        data_by_symbol[sym] = sd
    return {
        "missing": len(missing),
        "fetched": len(manifest.get("success") or []),
        "failed": len(manifest.get("failed") or []),
        "failed_symbols": [f.get("symbol") for f in (manifest.get("failed") or [])[:20]],
    }


def _load_v7_context_for_date(
    symbols: list[str],
    scan_date: str,
) -> tuple[dict[str, float | None], dict[str, float], dict[str, float | None]]:
    delivery = load_delivery_pct_by_symbol(
        symbols, as_of_date=scan_date, nse_cache_dir=NSE_CACHE_DIR,
    )
    sector_lead = load_sector_lead_scores(symbols, as_of_date=scan_date)
    free_float = load_free_float_pct_by_symbol(symbols, as_of_date=scan_date)
    return delivery, sector_lead, free_float


def _refresh_stale_caches(universe: list[dict[str, Any]], min_last_date: str) -> dict[str, Any]:
    stale: list[dict[str, Any]] = []
    for entry in universe:
        sym = entry["symbol"]
        ticker = entry["yahoo_ticker"]
        df, err = fetch_yahoo_history(ticker, range_str=RANGE_STR, min_bars=MIN_HISTORY_BARS)
        if df and not err:
            last = bar_dates_from_df(df)[-1] if df.get("timestamp") else ""
            if last < min_last_date:
                stale.append(entry)
        else:
            stale.append(entry)

    removed = 0
    for entry in stale:
        cache_path = _yahoo_backtest_cache_path(entry["yahoo_ticker"], RANGE_STR)
        if os.path.isfile(cache_path):
            os.remove(cache_path)
            removed += 1

    refetched = 0
    failed = 0
    if stale:
        data, manifest = fetch_universe_history(stale, range_str=RANGE_STR, warm_session=True)
        refetched = len(data)
        failed = len(manifest.get("failed") or [])

    return {
        "stale_symbols": len(stale),
        "cache_files_removed": removed,
        "refetched": refetched,
        "refetch_failed": failed,
    }


def _fetch_supabase_rows(client, min_scan_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 1000
    cols = (
        "scan_date,run_id,ticker,tier,passed,fail_reason,signal_tier,"
        "persistence_score,composite_rank,liquidity_quality,breakout_stage,"
        "base_score,pass_paths,risk_flags,pct_change,vol_mult,rsi_14,adx_14,"
        "entry_low,entry_high,stop_loss,target_price,inserted_at"
    )
    while True:
        resp = (
            client.table("breakout_stock_analysis")
            .select(cols)
            .gte("scan_date", min_scan_date)
            .order("scan_date", desc=False)
            .range(offset, offset + page - 1)
            .execute()
        )
        chunk = resp.data or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def _pick_old_run_per_day(rows_by_date: dict[str, list[dict]]) -> dict[str, dict[str, Any]]:
    """Pick canonical OLD run per scan_date: prefer pre-v7 (no signal_tier/composite_rank)."""
    out: dict[str, dict[str, Any]] = {}
    for scan_date, day_rows in rows_by_date.items():
        by_run: dict[str, list[dict]] = defaultdict(list)
        for r in day_rows:
            by_run[r["run_id"]].append(r)
        candidates = []
        for run_id, rr in by_run.items():
            v7_count = sum(
                1 for x in rr if x.get("signal_tier") or x.get("composite_rank") is not None
            )
            pass_count = sum(1 for x in rr if x.get("passed"))
            candidates.append((v7_count, -pass_count, -len(rr), run_id, rr))
        candidates.sort()
        if not candidates:
            continue
        v7_count, _, _, run_id, rr = candidates[0]
        out[scan_date] = {
            "run_id": run_id,
            "rows": rr,
            "v7_populated_rows": v7_count,
            "is_pre_v7": v7_count == 0 and len(rr) >= 50,
        }
    return out


def _replay_day_evaluations(
    universe: list[dict[str, Any]],
    data_by_symbol: dict[str, dict[str, Any]],
    trading_dates: list[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    date_set = set(trading_dates)
    per_day: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in trading_dates}
    all_syms = [e["symbol"] for e in universe]
    turnover_by_sym = {e["symbol"]: e.get("liquidity_turnover_lacs_avg") for e in universe}

    v7_by_date: dict[str, tuple[dict, dict, dict]] = {}
    for scan_date in trading_dates:
        v7_by_date[scan_date] = _load_v7_context_for_date(all_syms, scan_date)

    for entry in universe:
        sym = entry["symbol"]
        sd = data_by_symbol.get(sym)
        if not sd:
            continue
        dates = sd["dates"]
        tier_key = sd["tier_key"]
        last_pass: int | None = None
        for idx in range(MIN_HISTORY_BARS - 1, len(dates)):
            d = dates[idx]
            delivery, sector_lead_map, free_float = v7_by_date.get(d, ({}, {}, {}))
            eval_result = evaluate_bars_as_of(
                sd["df"],
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
            if d in date_set:
                per_day[d][sym] = {
                    "passed": bool(eval_result.get("passed")),
                    "signal_tier": eval_result.get("signal_tier"),
                    "fail_reason": eval_result.get("fail_reason"),
                    "v7_watch_reason": eval_result.get("v7_watch_reason"),
                    "composite_rank": eval_result.get("composite_rank"),
                    "liquidity_quality": eval_result.get("liquidity_quality"),
                    "persistence_score": eval_result.get("persistence_score"),
                    "breakout_stage": eval_result.get("breakout_stage"),
                    "pct_change": eval_result.get("pct_change"),
                    "vol_mult": eval_result.get("vol_mult"),
                    "bar_idx": idx,
                }
    return per_day


def _classify_old(row: dict[str, Any]) -> str:
    if row.get("passed"):
        return "PASS"
    if row.get("signal_tier") == "WATCH":
        return "WATCH"
    return "FAIL"


def _classify_new(ev: dict[str, Any]) -> str:
    if ev.get("passed"):
        return "PASS"
    if ev.get("signal_tier") == SIGNAL_TIER_WATCH:
        return "WATCH"
    return "FAIL"


def _aggregate_stop_rates(all_signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Stop-out rate: first_exit=loss or horizon stop_hit before target."""
    n = len(all_signals)
    if not n:
        return {"stop_rate_any": None, "stop_rate_t15": None, "count": 0}
    stop_any = 0
    stop_t15 = 0
    cov_t15 = 0
    for sig in all_signals:
        outcome = sig.get("outcome") or {}
        if outcome.get("first_exit") == "loss":
            stop_any += 1
        hz15 = (outcome.get("horizons") or {}).get("t15") or {}
        if hz15.get("result") not in (None, "insufficient_bars"):
            cov_t15 += 1
            if hz15.get("reason") == "stop_hit" or hz15.get("win") is False and hz15.get("result") == "loss":
                stop_t15 += 1
    return {
        "count": n,
        "stop_rate_any": round(100.0 * stop_any / n, 2),
        "stop_rate_t15": round(100.0 * stop_t15 / cov_t15, 2) if cov_t15 else None,
        "stop_any_count": stop_any,
        "stop_t15_count": stop_t15,
    }


def _forward_for_pass_signals(
    universe: list[dict],
    data_by_symbol: dict[str, dict],
    signals: list[tuple[str, str, dict]],
    v7_by_date: dict[str, tuple[dict, dict, dict]] | None = None,
) -> dict[str, Any]:
    turnover_by_sym = {e["symbol"]: e.get("liquidity_turnover_lacs_avg") for e in universe}
    all_sigs = []
    for sym, scan_date, ev in signals:
        sd = data_by_symbol.get(sym)
        if not sd:
            continue
        idx = ev.get("bar_idx")
        if idx is None:
            continue
        delivery, sector_lead_map, free_float = (v7_by_date or {}).get(scan_date, ({}, {}, {}))
        full_eval = evaluate_bars_as_of(
            sd["df"],
            idx,
            sd["tier_key"],
            bhav_turnover_lacs=turnover_by_sym.get(sym),
            delivery_pct=delivery.get(sym),
            free_float_pct=free_float.get(sym),
            sector_lead=sector_lead_map.get(sym),
        )
        outcome = validate_forward_path(
            sd["df"],
            idx,
            entry=full_eval["latest_price"],
            stop=full_eval["sl_price"],
            target=full_eval["target_price"],
        )
        all_sigs.append({"symbol": sym, "signal_date": scan_date, "outcome": outcome})
    agg = _aggregate_hit_rates(all_sigs)
    agg.update(_aggregate_stop_rates(all_sigs))
    return agg


def _dist(values: list[float | None]) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"count": 0, "mean": None, "median": None}
    return {"count": len(clean), "mean": round(mean(clean), 4), "median": round(median(clean), 4)}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Breakout old vs new efficacy reconciliation (~3 months)")
    p.add_argument(
        "--min-scan-date",
        default=DEFAULT_MIN_SCAN,
        help=f"Earliest Supabase scan_date (default {DEFAULT_MIN_SCAN})",
    )
    p.add_argument(
        "--full-universe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use full Nifty Smallcap 100 + Microcap 250 (~350 stocks)",
    )
    p.add_argument(
        "--trading-days",
        type=int,
        default=TRADING_DAYS_TARGET,
        help=f"Max trading sessions to reconcile (default {TRADING_DAYS_TARGET})",
    )
    p.add_argument(
        "--output-stem",
        default="efficacy_reconciliation_3m",
        help="Output file stem under output/breakoutcheck/",
    )
    p.add_argument(
        "--skip-cache-refresh",
        action="store_true",
        help="Skip stale Yahoo cache refresh (faster when cache is warm)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    t0 = time.monotonic()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY required", file=sys.stderr)
        return 1
    client = create_client(supabase_url, supabase_key)

    sb_rows = _fetch_supabase_rows(client, args.min_scan_date)

    rows_by_date: dict[str, list[dict]] = defaultdict(list)
    for r in sb_rows:
        rows_by_date[r["scan_date"]].append(r)

    old_runs = _pick_old_run_per_day(rows_by_date)
    trading_days_target = args.trading_days
    all_scan_dates = sorted(old_runs.keys())
    trading_dates = all_scan_dates[-trading_days_target:]

    if args.full_universe:
        universe_payload = build_full_universe()
        universe = universe_payload["stocks"]
        universe_mode = "full_nifty_smallcap100_microcap250"
        _enrich_universe_bhav_liquidity(universe)
    else:
        meta: dict[str, dict[str, Any]] = {}
        for r in sb_rows:
            sym = r["ticker"]
            if sym in meta:
                continue
            tier_label = r.get("tier") or FILTERS["SMALL_CAP_100"]["type"]
            meta[sym] = {
                "symbol": sym,
                "yahoo_ticker": f"{sym}.NS",
                "tier_key": _tier_key_from_label(tier_label),
                "tier_label": tier_label,
                "liquidity_turnover_lacs_avg": None,
            }
        universe = sorted(meta.values(), key=lambda x: x["symbol"])
        universe_mode = "supabase_scan_universe"
        universe_payload = {"total": len(universe)}

    cache_refresh = {"skipped": True}
    if not args.skip_cache_refresh and trading_dates:
        cache_refresh = _refresh_stale_caches(universe, min_last_date=trading_dates[-1])

    data_by_symbol = load_cached_universe_history(universe, range_str=RANGE_STR)
    cache_fetch = _fetch_missing_cache(universe, data_by_symbol, range_str=RANGE_STR)

    all_bar_dates: set[str] = set()
    for sd in data_by_symbol.values():
        all_bar_dates.update(sd["dates"])
    trading_dates = [d for d in trading_dates if d in all_bar_dates][-trading_days_target:]

    v7_by_date: dict[str, tuple[dict, dict, dict]] = {}
    all_syms = [e["symbol"] for e in universe]
    for scan_date in trading_dates:
        v7_by_date[scan_date] = _load_v7_context_for_date(all_syms, scan_date)

    new_per_day = _replay_day_evaluations(universe, data_by_symbol, trading_dates)

    daily_metrics = []
    transition_counts: dict[str, int] = defaultdict(int)
    old_pass_signals: list[tuple[str, str, dict]] = []
    new_pass_signals: list[tuple[str, str, dict]] = []

    total_old_pass = total_old_watch = total_old_fail = 0
    total_new_pass = total_new_watch = total_new_fail = 0
    old_liq: list[float] = []
    new_liq: list[float] = []
    old_comp: list[float] = []
    new_comp: list[float] = []

    stricter_demotions: list[dict] = []
    looser_promotions: list[dict] = []
    pre_v7_dates: list[str] = []
    v7_only_dates: list[str] = []

    for scan_date in trading_dates:
        old_pack = old_runs.get(scan_date)
        if not old_pack:
            continue
        if old_pack.get("is_pre_v7"):
            pre_v7_dates.append(scan_date)
        else:
            v7_only_dates.append(scan_date)

        old_by_ticker = {r["ticker"]: r for r in old_pack["rows"]}
        new_by_ticker = new_per_day.get(scan_date, {})
        tickers = sorted(set(old_by_ticker) & set(new_by_ticker) & set(data_by_symbol))

        d_old_pass = d_old_watch = d_old_fail = 0
        d_new_pass = d_new_watch = d_new_fail = 0
        for t in tickers:
            oc = _classify_old(old_by_ticker[t])
            nc = _classify_new(new_by_ticker[t])
            transition_counts[f"{oc}->{nc}"] += 1
            if oc == "PASS":
                d_old_pass += 1
                old_pass_signals.append((t, scan_date, new_by_ticker[t]))
            elif oc == "WATCH":
                d_old_watch += 1
            else:
                d_old_fail += 1
            if nc == "PASS":
                d_new_pass += 1
                new_pass_signals.append((t, scan_date, new_by_ticker[t]))
            elif nc == "WATCH":
                d_new_watch += 1
            else:
                d_new_fail += 1

            if oc == "PASS" and nc in ("FAIL", "WATCH"):
                stricter_demotions.append({
                    "scan_date": scan_date,
                    "ticker": t,
                    "old": oc,
                    "new": nc,
                    "fail_reason": new_by_ticker[t].get("fail_reason"),
                    "v7_watch_reason": new_by_ticker[t].get("v7_watch_reason"),
                })
            if oc in ("FAIL", "WATCH") and nc == "PASS":
                looser_promotions.append({
                    "scan_date": scan_date,
                    "ticker": t,
                    "old": oc,
                    "new": nc,
                })

            lq = old_by_ticker[t].get("liquidity_quality")
            if lq is not None:
                old_liq.append(float(lq))
            nlq = new_by_ticker[t].get("liquidity_quality")
            if nlq is not None:
                new_liq.append(float(nlq))
            cr = old_by_ticker[t].get("composite_rank")
            if cr is not None:
                old_comp.append(float(cr))
            ncr = new_by_ticker[t].get("composite_rank")
            if ncr is not None:
                new_comp.append(float(ncr))

        total_old_pass += d_old_pass
        total_old_watch += d_old_watch
        total_old_fail += d_old_fail
        total_new_pass += d_new_pass
        total_new_watch += d_new_watch
        total_new_fail += d_new_fail

        daily_metrics.append({
            "scan_date": scan_date,
            "run_id": old_pack["run_id"],
            "is_pre_v7": old_pack.get("is_pre_v7", False),
            "overlap_tickers": len(tickers),
            "old": {"pass": d_old_pass, "watch": d_old_watch, "fail": d_old_fail},
            "new": {"pass": d_new_pass, "watch": d_new_watch, "fail": d_new_fail},
            "v7_populated_rows_old_run": old_pack["v7_populated_rows"],
        })

    old_fwd = _forward_for_pass_signals(universe, data_by_symbol, old_pass_signals, v7_by_date)
    new_fwd = _forward_for_pass_signals(universe, data_by_symbol, new_pass_signals, v7_by_date)

    demotion_fwd = _forward_for_pass_signals(
        universe,
        data_by_symbol,
        [(d["ticker"], d["scan_date"], new_per_day[d["scan_date"]][d["ticker"]]) for d in stricter_demotions[:500]],
        v7_by_date,
    )
    promotion_fwd = _forward_for_pass_signals(
        universe,
        data_by_symbol,
        [(d["ticker"], d["scan_date"], new_per_day[d["scan_date"]][d["ticker"]]) for d in looser_promotions[:500]],
        v7_by_date,
    )

    # Pre-v7 only forward (cleanest old-vs-new comparison)
    pre_v7_old_pass = [(t, d, ev) for t, d, ev in old_pass_signals if d in pre_v7_dates]
    pre_v7_new_pass = [(t, d, ev) for t, d, ev in new_pass_signals if d in pre_v7_dates]
    pre_v7_old_fwd = _forward_for_pass_signals(universe, data_by_symbol, pre_v7_old_pass, v7_by_date)
    pre_v7_new_fwd = _forward_for_pass_signals(universe, data_by_symbol, pre_v7_new_pass, v7_by_date)

    runtime_sec = round(time.monotonic() - t0, 1)
    v7_context_coverage = {
        "delivery_symbols_with_data": sum(
            1 for d in trading_dates for sym in all_syms if (v7_by_date[d][0].get(sym)) is not None
        ),
        "sector_lead_symbols_with_data": sum(
            1 for d in trading_dates for sym in all_syms if sym in v7_by_date[d][1]
        ),
        "free_float_symbols_with_data": sum(
            1 for d in trading_dates for sym in all_syms if (v7_by_date[d][2].get(sym)) is not None
        ),
    }

    branch = _git_branch()
    result: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "branch": branch,
            "runtime_seconds": runtime_sec,
            "universe": {
                "mode": universe_mode,
                "total": len(universe),
                "small_cap_count": universe_payload.get("small_cap_count"),
                "micro_cap_count": universe_payload.get("micro_cap_count"),
                "cached_symbols": len(data_by_symbol),
                "cache_refresh": cache_refresh,
                "cache_fetch": cache_fetch,
            },
            "supabase_history": {
                "rows_fetched": len(sb_rows),
                "scan_dates_available": len(rows_by_date),
                "first_supabase_scan": min(rows_by_date) if rows_by_date else None,
                "last_supabase_scan": max(rows_by_date) if rows_by_date else None,
                "requested_min_scan_date": args.min_scan_date,
                "pre_v7_scan_dates": pre_v7_dates,
                "v7_persisted_scan_dates": v7_only_dates,
            },
            "window": {
                "trading_days": len(trading_dates),
                "first_scan_date": trading_dates[0] if trading_dates else None,
                "last_scan_date": trading_dates[-1] if trading_dates else None,
                "yahoo_range": RANGE_STR,
            },
            "old_logic_identification": (
                "Supabase breakout_stock_analysis canonical run per scan_date "
                "(prefers pre-v7 runs without signal_tier/composite_rank populated)"
            ),
            "new_logic_identification": (
                f"Local replay via evaluate_bars_as_of on {branch} with "
                "delivery_pct, sector_lead, free_float_pct, bhav_turnover where available"
            ),
            "v7_context_coverage": v7_context_coverage,
            "gha_backtest": {
                "run_id": GHA_RUN_ID,
                "run_url": f"https://github.com/arunurun/titan/actions/runs/{GHA_RUN_ID}",
                "artifact_integration": (
                    "When run completes, download artifact breakout-backtest-* from the workflow "
                    "run page and compare output/breakoutcheck/full_universe/backtest_summary.json "
                    "hit rates against this reconciliation's forward_validation.new_pass_signals."
                ),
            },
        },
        "cohort_totals": {
            "old": {"pass": total_old_pass, "watch": total_old_watch, "fail": total_old_fail},
            "new": {"pass": total_new_pass, "watch": total_new_watch, "fail": total_new_fail},
            "pass_delta": total_new_pass - total_old_pass,
            "watch_delta": total_new_watch - total_old_watch,
        },
        "transitions": dict(sorted(transition_counts.items(), key=lambda x: -x[1])),
        "stricter_demotions_count": len(stricter_demotions),
        "looser_promotions_count": len(looser_promotions),
        "stricter_demotions_sample": stricter_demotions[:30],
        "looser_promotions_sample": looser_promotions[:30],
        "forward_validation": {
            "old_pass_signals": _fwd_summary(old_fwd),
            "new_pass_signals": _fwd_summary(new_fwd),
            "old_pass_demoted_by_new": _fwd_summary(demotion_fwd),
            "new_pass_promoted_vs_old": _fwd_summary(promotion_fwd),
            "pre_v7_only": {
                "old_pass_signals": _fwd_summary(pre_v7_old_fwd),
                "new_pass_signals": _fwd_summary(pre_v7_new_fwd),
            },
        },
        "quality_distributions": {
            "liquidity_quality": {"old": _dist(old_liq), "new": _dist(new_liq)},
            "composite_rank": {"old": _dist(old_comp), "new": _dist(new_comp)},
        },
        "daily_metrics": daily_metrics,
        "limitations": [],
    }

    if len(data_by_symbol) < len(universe):
        result["limitations"].append(
            f"Cache miss: {len(universe) - len(data_by_symbol)} universe symbols missing from Yahoo backtest cache"
        )
    if len(trading_dates) < trading_days_target:
        result["limitations"].append(
            f"Supabase only has {len(rows_by_date)} scan dates in range "
            f"({min(rows_by_date) if rows_by_date else 'n/a'} .. "
            f"{max(rows_by_date) if rows_by_date else 'n/a'}); "
            f"requested up to {trading_days_target} trading days (~3 months)"
        )
    if len(pre_v7_dates) <= 1:
        result["limitations"].append(
            f"Only {len(pre_v7_dates)} pre-v7 scan date(s) ({pre_v7_dates}) — "
            "most comparisons are production v7 snapshot vs current replay, not true pre-v7 vs v7"
        )
    if v7_context_coverage.get("sector_lead_symbols_with_data", 0) == 0:
        result["limitations"].append(
            "sector_lead context unavailable for replay window — v7 sector filter may not match production"
        )

    stem = args.output_stem
    json_path = OUT_DIR / f"{stem}.json"
    md_path = OUT_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md_path.write_text(_build_markdown(result), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(json.dumps({
        "runtime_seconds": runtime_sec,
        "universe": len(universe),
        "cached": len(data_by_symbol),
        "trading_days": len(trading_dates),
        "old_pass": total_old_pass,
        "new_pass": total_new_pass,
        "new_watch": total_new_watch,
        "demotions": len(stricter_demotions),
        "promotions": len(looser_promotions),
    }, indent=2))
    return 0


def _pct(val: Any) -> str:
    if val is None:
        return "n/a"
    return f"{val}%"


def _fwd_summary(fwd: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": fwd.get("total_signals", 0),
        "hit_rate_t5": fwd.get("hit_rate_t5"),
        "hit_rate_t10": fwd.get("hit_rate_t10"),
        "hit_rate_t15": fwd.get("hit_rate_t15"),
        "mfe_hit_8_rate": fwd.get("mfe_hit_8_rate"),
        "close_hit_8pct_rate": fwd.get("close_hit_8pct_rate"),
        "stop_rate_any": fwd.get("stop_rate_any"),
        "stop_rate_t15": fwd.get("stop_rate_t15"),
    }


def _build_markdown(r: dict[str, Any]) -> str:
    meta = r["meta"]
    cohort = r["cohort_totals"]
    fwd = r["forward_validation"]
    sb = meta.get("supabase_history") or {}
    lines = [
        "# Breakout Logic Efficacy Reconciliation — ~3 Month Window",
        "",
        f"- **Generated**: {meta['generated_at']}",
        f"- **Branch**: `{meta['branch']}`",
        f"- **Requested range**: ≥ {sb.get('requested_min_scan_date', 'n/a')}",
        f"- **Supabase coverage**: {sb.get('first_supabase_scan')} .. {sb.get('last_supabase_scan')} "
        f"({sb.get('scan_dates_available', 0)} scan dates)",
        f"- **Reconciled window**: {meta['window']['first_scan_date']} .. {meta['window']['last_scan_date']} "
        f"({meta['window']['trading_days']} trading days)",
        f"- **Runtime**: {meta.get('runtime_seconds', 'n/a')}s",
        f"- **Universe**: {meta['universe']['mode']} — {meta['universe']['cached_symbols']}/"
        f"{meta['universe']['total']} symbols with cached bars",
        f"- **Pre-v7 dates**: {', '.join(sb.get('pre_v7_scan_dates') or []) or 'none'}",
        "",
        "## Executive Summary",
        "",
    ]

    op = cohort["old"]["pass"]
    np_ = cohort["new"]["pass"]
    nw = cohort["new"]["watch"]
    delta = cohort["pass_delta"]
    if np_ < op:
        lines.append(
            f"New logic is **stricter**: PASS fell **{op} → {np_}** ({delta:+d}), "
            f"with **{nw}** WATCH-tier signals absorbing borderline cases."
        )
    elif np_ > op:
        lines.append(
            f"New logic is **more permissive**: PASS rose **{op} → {np_}** ({delta:+d})."
        )
    else:
        lines.append(f"PASS counts unchanged at **{op}**; WATCH bucket **{nw}**.")

    old_h5 = fwd["old_pass_signals"].get("hit_rate_t5")
    new_h5 = fwd["new_pass_signals"].get("hit_rate_t5")
    old_mfe = fwd["old_pass_signals"].get("mfe_hit_8_rate")
    new_mfe = fwd["new_pass_signals"].get("mfe_hit_8_rate")
    if old_h5 is not None and new_h5 is not None:
        delta_h5 = round(new_h5 - old_h5, 2)
        lines.append(
            f"T+5 target hit rate: **{old_h5}% → {new_h5}%** ({delta_h5:+.2f} pp) on PASS cohorts."
        )
    if old_mfe is not None and new_mfe is not None:
        delta_mfe = round(new_mfe - old_mfe, 2)
        lines.append(
            f"MFE ≥8% rate: **{old_mfe}% → {new_mfe}%** ({delta_mfe:+.2f} pp)."
        )

    dem = r.get("stricter_demotions_count", 0)
    prom = r.get("looser_promotions_count", 0)
    lines.append(
        f"Quality filter: **{dem}** old PASS demoted (FAIL/WATCH); **{prom}** new PASS promoted (recall)."
    )

    pre = fwd.get("pre_v7_only") or {}
    if pre.get("old_pass_signals", {}).get("count"):
        po = pre["old_pass_signals"]
        pn = pre["new_pass_signals"]
        lines.append(
            f"Pre-v7 only ({sb.get('pre_v7_scan_dates')}): old PASS T+5 **{_pct(po.get('hit_rate_t5'))}** "
            f"(n={po.get('count')}) vs new PASS **{_pct(pn.get('hit_rate_t5'))}** (n={pn.get('count')})."
        )

    lines.extend([
        "",
        "## Cohort Totals (overlap universe)",
        "",
        "| Tier | Old (Supabase) | New (replay) | Δ |",
        "| :--- | ---: | ---: | ---: |",
        f"| PASS | {cohort['old']['pass']} | {cohort['new']['pass']} | {cohort['pass_delta']:+d} |",
        f"| WATCH | {cohort['old']['watch']} | {cohort['new']['watch']} | {cohort['watch_delta']:+d} |",
        f"| FAIL | {cohort['old']['fail']} | {cohort['new']['fail']} | "
        f"{cohort['new']['fail'] - cohort['old']['fail']:+d} |",
        "",
        "## Transition Matrix (old → new)",
        "",
        "| Transition | Count |",
        "| :--- | ---: |",
    ])
    for k, v in (r.get("transitions") or {}).items():
        lines.append(f"| {k} | {v} |")

    lines.extend([
        "",
        "## Forward Validation (PASS signals)",
        "",
        "| Metric | Old PASS | New PASS | Old-PASS demoted | New-PASS promoted |",
        "| :--- | ---: | ---: | ---: | ---: |",
    ])
    o, n, d, p = (
        fwd["old_pass_signals"],
        fwd["new_pass_signals"],
        fwd["old_pass_demoted_by_new"],
        fwd.get("new_pass_promoted_vs_old") or {},
    )
    lines.append(f"| Signal count | {o['count']} | {n['count']} | {d['count']} | {p.get('count', 0)} |")
    for metric, key in [
        ("T+5 hit rate", "hit_rate_t5"),
        ("T+10 hit rate", "hit_rate_t10"),
        ("T+15 hit rate", "hit_rate_t15"),
        ("MFE ≥8% rate", "mfe_hit_8_rate"),
        ("Stop rate (any)", "stop_rate_any"),
        ("Stop rate (T+15)", "stop_rate_t15"),
    ]:
        lines.append(
            f"| {metric} | {_pct(o.get(key))} | {_pct(n.get(key))} | "
            f"{_pct(d.get(key))} | {_pct(p.get(key))} |"
        )

    lines.extend([
        "",
        f"## Stricter Demotions (old PASS → new FAIL/WATCH): **{r['stricter_demotions_count']}**",
        "",
    ])
    for item in (r.get("stricter_demotions_sample") or [])[:12]:
        lines.append(
            f"- {item['scan_date']} **{item['ticker']}**: {item['old']}→{item['new']} "
            f"({item.get('v7_watch_reason') or item.get('fail_reason')})"
        )

    lines.extend([
        "",
        f"## Looser Promotions (old FAIL/WATCH → new PASS): **{r['looser_promotions_count']}**",
        "",
    ])
    for item in (r.get("looser_promotions_sample") or [])[:12]:
        lines.append(f"- {item['scan_date']} **{item['ticker']}**: {item['old']}→{item['new']}")

    lines.extend([
        "",
        "## Daily Breakdown",
        "",
        "| Date | Pre-v7 | Overlap | Old P/W/F | New P/W/F |",
        "| :--- | :---: | ---: | :--- | :--- |",
    ])
    for d in r.get("daily_metrics") or []:
        o_ = d["old"]
        n_ = d["new"]
        lines.append(
            f"| {d['scan_date']} | {'✓' if d.get('is_pre_v7') else ''} | {d['overlap_tickers']} | "
            f"{o_['pass']}/{o_['watch']}/{o_['fail']} | {n_['pass']}/{n_['watch']}/{n_['fail']} |"
        )

    gha = meta.get("gha_backtest") or {}
    lines.extend([
        "",
        "## GHA Backtest Integration",
        "",
        f"- Run: [{gha.get('run_id')}]({gha.get('run_url')})",
        f"- {gha.get('artifact_integration', '')}",
        "",
    ])

    lim = r.get("limitations") or []
    if lim:
        lines.extend(["## Limitations", ""])
        for x in lim:
            lines.append(f"- {x}")

    lines.extend([
        "",
        "## Methodology",
        "",
        meta.get("old_logic_identification", ""),
        meta.get("new_logic_identification", ""),
        "",
        "Comparison limited to tickers present in both Supabase old run and full-universe replay.",
        "Forward validation uses production stop/target path model on signal-day close.",
        "",
        "*Educational reconciliation only; not investment advice.*",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
