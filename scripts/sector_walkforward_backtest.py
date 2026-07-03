#!/usr/bin/env python3
"""Sector walk-forward backtest CLI (baseline / production / 3b / 3c / tuned).

Examples:
  python scripts/sector_walkforward_backtest.py --sector pharma_healthcare --start 2026-05-01 --end 2026-06-30 --arm baseline
  python scripts/sector_walkforward_backtest.py --sector auto --start 2026-05-01 --end 2026-06-30 --compare
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
_SCRIPTS = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if p != str(_SCRIPTS)]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sector_tuned import sector_report_dir  # noqa: E402
from sector_walkforward_backtest import (  # noqa: E402
    COMPARISON_ARMS,
    FUSION_COMPARISON_ARMS,
    _arm_kwargs,
    format_final_comparison_report,
    format_fusion_comparison_report,
    run_arm_comparison,
    run_fusion_arm_comparison,
    run_walkforward,
    write_final_comparison_artifact,
)


def _fmt(v) -> str:
    if v is None:
        return "  -  "
    if isinstance(v, float) and math.isnan(v):
        return " nan "
    if isinstance(v, float):
        return f"{v:6.2f}"
    return str(v)


def format_report(result: dict) -> str:
    p = result.get("params", {})
    horizons = p.get("horizons") or []
    c = result.get("cohort") or {}
    buy = c.get("buy_forward") or {}
    cov = result.get("data_coverage") or {}

    lines = [
        f"=== {p.get('sector_key')} sector walk-forward backtest ===",
        (
            f"range={p.get('start')}..{p.get('end')} arm={p.get('profile_arm')} "
            f"horizons={horizons} gap<={p.get('max_gap_days')}d"
        ),
        f"observations={c.get('observations')} buy_signals={c.get('buy_signals')} "
        f"labels={c.get('label_counts')}",
        (
            f"coverage: declared={cov.get('symbols_declared')} with_rows={cov.get('symbols_with_rows')} "
            f"missing={len(cov.get('symbols_missing') or [])} sparse={len(cov.get('symbols_sparse_lt5_sessions') or [])}"
        ),
        "",
        f"{'COHORT':<10}{'cov':>5} {'win%':>6} {'avg%':>7}",
    ]
    for h in horizons:
        hh = c.get(f"horizon_{h}d") or {}
        lines.append(
            f"all@{h:>2}d   {hh.get('coverage', 0):>5} "
            f"{_fmt(hh.get('win_rate_pct')):>6} {_fmt(hh.get('avg_return_pct')):>7}"
        )
    lines.append("")
    lines.append("Buy-rated forward returns:")
    for h in horizons:
        hh = buy.get(f"horizon_{h}d") or {}
        lines.append(
            f"buy@{h:>2}d   {hh.get('coverage', 0):>5} "
            f"{_fmt(hh.get('win_rate_pct')):>6} {_fmt(hh.get('avg_return_pct')):>7}"
        )
    return "\n".join(lines)


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Sector signal_v2 walk-forward backtest.")
    ap.add_argument("--sector", required=True, help="sector_key e.g. pharma_healthcare, auto")
    ap.add_argument("--start", default="2026-06-01", help="signal-date range start (YYYY-MM-DD)")
    ap.add_argument("--end", default="2026-06-30", help="signal-date range end (YYYY-MM-DD)")
    ap.add_argument("--horizons", default="1,5,10,15", help="forward sessions, e.g. 1,5,10,15")
    ap.add_argument(
        "--arm",
        choices=["baseline", "production", "3b", "3c", "tuned"],
        default="baseline",
        help="baseline=profile-off; production=sector profile; 3b/3c/tuned=mold presets",
    )
    ap.add_argument(
        "--compare",
        action="store_true",
        help="run all comparison arms and write comparison markdown",
    )
    ap.add_argument(
        "--compare-fusion",
        action="store_true",
        help="run fusion_off vs fusion_on arms (production profile)",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    ap.add_argument("--output-dir", type=Path, default=None, help="directory for JSON + markdown artifacts")
    ap.add_argument("--buffer-days", type=int, default=None, help="calendar days fetched past --end")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    output_dir = args.output_dir or sector_report_dir(args.sector)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.compare_fusion:
        report = run_fusion_arm_comparison(
            sector_key=args.sector,
            start=args.start,
            end=args.end,
            horizons=horizons,
            arms=FUSION_COMPARISON_ARMS,
            buffer_days=args.buffer_days,
        )
        cmp_path = output_dir / f"comparison_fusion_{args.sector}_{args.start.replace('-', '')[:6]}.md"
        cmp_path.write_text(format_fusion_comparison_report(report), encoding="utf-8")
        json_path = output_dir / f"comparison_fusion_{args.sector}_{args.start.replace('-', '')[:6]}.json"
        json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(format_fusion_comparison_report(report))
            print(f"\nWrote {cmp_path}")
            print(f"Wrote {json_path}")
        return 0

    if args.compare:
        report = run_arm_comparison(
            sector_key=args.sector,
            start=args.start,
            end=args.end,
            horizons=horizons,
            arms=COMPARISON_ARMS,
            buffer_days=args.buffer_days,
            report_dir=output_dir,
        )
        cmp_path = write_final_comparison_artifact(report, output_dir)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(format_final_comparison_report(report))
            print(f"\nWrote {cmp_path}")
        return 0

    kwargs = _arm_kwargs(
        args.arm,
        args.sector,
        report_dir=output_dir,
        start=args.start,
        end=args.end,
    )
    result = run_walkforward(
        sector_key=args.sector,
        start=args.start,
        end=args.end,
        horizons=horizons,
        buffer_days=args.buffer_days,
        **kwargs,
    )

    stamp = args.start.replace("-", "")[:6]
    tag = args.arm
    sec = result["params"]["sector_key"]
    json_path = output_dir / f"{sec}_backtest_{tag}_{stamp}.json"
    md_path = output_dir / f"{sec}_backtest_{tag}_{stamp}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path.write_text(format_report(result) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))
        print(f"\nWrote {json_path}")
        print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
