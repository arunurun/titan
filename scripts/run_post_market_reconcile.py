from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reconcile_runner import run_reconcile_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Supabase-only post-market reconcile.")
    parser.add_argument(
        "--scope",
        type=str,
        choices=("all-stocks", "sector"),
        default="all-stocks",
        help="Reconcile scope. Default is all-stocks (all active sectors/universe).",
    )
    parser.add_argument("--sector", type=str, default="defence")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Recompute/persist reconcile summaries from stored symbol history for recent N trade dates.",
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="Skip reconcile report generation and only persist backfill reconcile summaries.",
    )
    args = parser.parse_args()

    all_stocks_scope = str(args.scope).strip().lower() == "all-stocks"
    sector = args.sector.strip().lower()
    if not all_stocks_scope and not sector:
        raise ValueError("sector must be a non-empty string when --scope=sector")

    if args.max_symbols is not None:
        print(
            "[reconcile] note: --max-symbols is ignored in Supabase-only reconcile mode "
            "(report is based on stored table inputs)."
        )
    if args.workers is not None:
        print(
            "[reconcile] note: --workers is ignored in Supabase-only reconcile mode "
            "(no live market execution)."
        )
    if args.backfill_only:
        run_reconcile_report(
            sector=sector if not all_stocks_scope else None,
            all_stocks=all_stocks_scope,
            backfill_days=max(1, int(args.backfill_days or 1)),
            generate_report=False,
            email_subject_prefix="Titan V12.0 reconcile backfill",
        )
        return 0
    run_reconcile_report(
        sector=sector if not all_stocks_scope else None,
        all_stocks=all_stocks_scope,
        backfill_days=max(0, int(args.backfill_days or 0)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
