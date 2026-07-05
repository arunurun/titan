#!/usr/bin/env python3
"""CLI for breakout scanner historical backtest.

Examples:
  python scripts/breakout_backtest.py --dry-run
  python scripts/breakout_backtest.py
  python scripts/breakout_backtest.py --symbols RELIANCE,TCS --top-n 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from breakout_backtest import (  # noqa: E402
    build_backtest_universe,
    build_full_universe,
    default_output_dir,
    run_backtest,
    run_missed_breakout_analysis,
    run_setup_backtest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Breakout scanner 6-month historical backtest.")
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Report output directory (default: {default_output_dir()})",
    )
    ap.add_argument(
        "--nse-cache",
        type=Path,
        default=ROOT / "temp" / "nse_cache",
        help="Bhavcopy cache for liquidity ranking",
    )
    ap.add_argument("--top-n", type=int, default=20, help="Top liquid names per tier (default 20)")
    ap.add_argument(
        "--full-universe",
        action="store_true",
        help="Use all Nifty Smallcap 100 + Microcap 250 constituents (~350 stocks)",
    )
    ap.add_argument(
        "--small",
        type=int,
        default=None,
        metavar="N",
        help="Use top-N liquid small-cap only (with --micro for partial universe)",
    )
    ap.add_argument(
        "--micro",
        type=int,
        default=None,
        metavar="N",
        help="Use top-N liquid micro-cap only (with --small for partial universe)",
    )
    ap.add_argument("--range", dest="range_str", default="6m", help="Yahoo chart range (default 6m)")
    ap.add_argument(
        "--prefer-supabase",
        dest="prefer_supabase",
        action="store_const",
        const=True,
        default=None,
        help="Prefer Supabase equity_ohlcv_daily (default: true when SUPABASE_* env set)",
    )
    ap.add_argument(
        "--no-prefer-supabase",
        dest="prefer_supabase",
        action="store_const",
        const=False,
        help="Force Yahoo fetch even when Supabase is configured",
    )
    ap.add_argument(
        "--symbols",
        default="",
        help="Comma-separated subset of universe symbols (for dry-run smoke tests)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Run first 5 universe symbols only (quick smoke test)",
    )
    ap.add_argument(
        "--universe-only",
        action="store_true",
        help="Build and print universe JSON without fetching Yahoo data",
    )
    ap.add_argument(
        "--missed-analysis",
        action="store_true",
        help=(
            "Missed-breakout report on cached Yahoo history (uses output-dir cache; "
            "no refetch unless cache missing)"
        ),
    )
    ap.add_argument(
        "--setup-backtest",
        action="store_true",
        help="Run PRE_BREAKOUT setup→breakout precision backtest (writes setup_backtest.md)",
    )
    ap.add_argument("--json", action="store_true", help="Print summary JSON to stdout")
    return ap.parse_args(argv)


def _resolve_universe(args: argparse.Namespace) -> dict[str, Any]:
    if args.full_universe:
        return build_full_universe()
    if args.small is not None or args.micro is not None:
        small_n = args.small if args.small is not None else 0
        micro_n = args.micro if args.micro is not None else 0
        small_payload = build_backtest_universe(
            nse_cache_dir=args.nse_cache, top_n=max(small_n, 1),
        ) if small_n > 0 else {"stocks": []}
        micro_payload = build_backtest_universe(
            nse_cache_dir=args.nse_cache, top_n=max(micro_n, 1),
        ) if micro_n > 0 else {"stocks": []}
        small_stocks = (small_payload.get("stocks") or [])[:small_n] if small_n > 0 else []
        micro_stocks = (micro_payload.get("stocks") or [])[:micro_n] if micro_n > 0 else []
        # Re-pick micro from second tier only
        if micro_n > 0:
            full = build_backtest_universe(nse_cache_dir=args.nse_cache, top_n=micro_n)
            micro_stocks = [s for s in full.get("stocks", []) if s.get("tier_key") == "MICRO_CAP_250"][:micro_n]
        if small_n > 0:
            full = build_backtest_universe(nse_cache_dir=args.nse_cache, top_n=small_n)
            small_stocks = [s for s in full.get("stocks", []) if s.get("tier_key") == "SMALL_CAP_100"][:small_n]
        stocks = small_stocks + micro_stocks
        return {
            "built_at": full.get("built_at") if small_n or micro_n else None,
            "universe_mode": "partial",
            "small_cap_count": len(small_stocks),
            "micro_cap_count": len(micro_stocks),
            "total": len(stocks),
            "stocks": stocks,
        }
    return build_backtest_universe(nse_cache_dir=args.nse_cache, top_n=args.top_n)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = args.output_dir or default_output_dir()
    if args.full_universe:
        out_dir = args.output_dir or (default_output_dir().parent / "breakoutcheck" / "full_universe")
        out_dir.mkdir(parents=True, exist_ok=True)

    universe_payload = _resolve_universe(args)

    if args.universe_only:
        fname = "universe_full.json" if args.full_universe else "universe_40.json"
        out_path = out_dir / fname
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(universe_payload, indent=2), encoding="utf-8")
        print(f"Universe written: {out_path} ({universe_payload['total']} stocks)")
        return 0

    if args.missed_analysis:
        analysis = run_missed_breakout_analysis(
            universe=universe_payload["stocks"],
            nse_cache_dir=args.nse_cache,
            top_n=args.top_n,
            range_str=args.range_str,
            output_dir=out_dir,
        )
        paths = analysis.get("paths") or {}
        print("=== Missed breakout analysis complete ===", flush=True)
        print(f"Missed opportunities: {analysis.get('total_missed_opportunities', 0)}", flush=True)
        print(f"Single-filter near misses: {analysis.get('total_single_filter_near_misses', 0)}", flush=True)
        print(f"Obvious blocked: {analysis.get('total_obvious_blocked', 0)}", flush=True)
        print(f"Report: {paths.get('markdown')}", flush=True)
        if args.json:
            print(json.dumps({
                "total_missed_opportunities": analysis.get("total_missed_opportunities"),
                "cohort_primary_fail_counts": analysis.get("cohort_primary_fail_counts"),
                "paths": paths,
            }, indent=2))
        return 0

    if args.setup_backtest:
        stock_filter: list[str] | None = None
        if args.dry_run:
            stock_filter = [s["symbol"] for s in universe_payload["stocks"][:5]]
        elif args.symbols.strip():
            stock_filter = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        setup_report = run_setup_backtest(
            universe=universe_payload["stocks"],
            nse_cache_dir=args.nse_cache,
            top_n=args.top_n,
            range_str=args.range_str,
            output_dir=out_dir,
            stock_filter=stock_filter,
            prefer_supabase=args.prefer_supabase,
        )
        paths = setup_report.get("paths") or {}
        summary = setup_report.get("summary") or {}
        print("=== Setup backtest complete ===", flush=True)
        print(f"Setup signals: {summary.get('total_setup_signals', 0)}", flush=True)
        for h in (5, 10, 15):
            prec = summary.get(f"precision_t{h}")
            print(f"Precision T+{h}: {prec}%" if prec is not None else f"Precision T+{h}: n/a", flush=True)
        print(f"Report: {paths.get('markdown')}", flush=True)
        if args.json:
            print(json.dumps({"summary": summary, "paths": paths}, indent=2))
        return 0

    stock_filter: list[str] | None = None
    if args.dry_run:
        stock_filter = [s["symbol"] for s in universe_payload["stocks"][:5]]
        print(f"Dry-run mode: {stock_filter}", flush=True)
    elif args.symbols.strip():
        stock_filter = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    report = run_backtest(
        universe=universe_payload["stocks"],
        nse_cache_dir=args.nse_cache,
        top_n=args.top_n,
        range_str=args.range_str,
        output_dir=out_dir,
        stock_filter=stock_filter,
        prefer_supabase=args.prefer_supabase,
    )

    summary = report.get("summary") or {}
    paths = report.get("paths") or {}
    print("=== Breakout backtest complete ===", flush=True)
    print(f"Signals: {summary.get('total_signals', 0)}", flush=True)
    for h in (5, 10, 15):
        hr = summary.get(f"hit_rate_t{h}")
        print(f"Hit rate T+{h}: {hr}%" if hr is not None else f"Hit rate T+{h}: n/a", flush=True)
    print(f"Fetch failures: {summary.get('stocks_failed', 0)}", flush=True)
    if summary.get("supabase_hits") is not None:
        print(f"Supabase OHLCV hits: {summary.get('supabase_hits', 0)}", flush=True)
        print(f"Yahoo fetches: {summary.get('yahoo_fetches', 0)}", flush=True)
    print(f"Report: {paths.get('markdown')}", flush=True)
    print(f"Duration: {(report.get('meta') or {}).get('duration_sec')}s", flush=True)

    if args.json:
        print(json.dumps({
            "summary": summary,
            "paths": paths,
            "meta": report.get("meta"),
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
