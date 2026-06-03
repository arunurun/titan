#!/usr/bin/env python3
"""A/B backtest harness for legacy vs signal_v2 (spec section 8).

Examples:
  python scripts/signal_v2_backtest.py --fixtures
  python scripts/signal_v2_backtest.py --csv path/to/features.csv
  python scripts/signal_v2_backtest.py --sector ai --lookback-days 45

Supabase mode needs Titan config (``load_config()`` / ``.env``) with Supabase URL+key,
and historical rows in ``symbol_daily_features`` (``TITAN_ENABLE_ANALYSIS_STORE=1`` runs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_v2_backtest import (  # noqa: E402
    BUILTIN_FIXTURE_ROWS,
    fetch_supabase_rows,
    format_report,
    load_csv_rows,
    run_legacy_vs_v2_ab,
)

FLAG_HELP = """
Environment flags (all default OFF unless noted):
  TITAN_SIGNAL_V2=1           Master switch — engage v2 engine (required for v2 path).
  TITAN_SIGNAL_V2_LAYER_A=1   Layer A data-quality (default ON when master is on).
  TITAN_SIGNAL_V2_LAYER_B=1   Layer B hard disqualifiers (default ON when master is on).
  TITAN_SIGNAL_V2_LAYER_C=1   Layer C graded evidence (default ON when master is on).
  TITAN_SIGNAL_V2_LAYER_D=1   Layer D context modifiers (default ON when master is on).
  TITAN_SIGNAL_V2_LAYER_E=1   Layer E mapping + hysteresis (default ON when master is on).
  TITAN_SIGV2_ENABLE_ACCUMULATE=1  Emit accumulate label (default OFF).
Per-layer ablation in the report toggles one layer flag off vs on while master stays on.
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Legacy vs signal_v2 A/B metrics on symbol_daily_features history.",
        epilog=FLAG_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--fixtures",
        action="store_true",
        help="Run built-in offline sample rows (no Supabase credentials).",
    )
    src.add_argument("--csv", type=str, metavar="PATH", help="CSV export of feature rows.")
    src.add_argument(
        "--sector",
        type=str,
        metavar="SECTOR",
        help="Fetch symbol_daily_features for sector from Supabase.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=45,
        help="Days of history when using --sector (default 45).",
    )
    parser.add_argument(
        "--flip-guardrail-pct",
        type=float,
        default=0.05,
        help="Max allowed v2 flip-rate above legacy mean (default 0.05 = 5%%).",
    )
    parser.add_argument(
        "--accumulate",
        action="store_true",
        help="Set TITAN_SIGV2_ENABLE_ACCUMULATE=1 during recompute (default off).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON report instead of human-readable text.",
    )
    args = parser.parse_args()

    if args.fixtures:
        rows = BUILTIN_FIXTURE_ROWS
    elif args.csv:
        rows = load_csv_rows(args.csv)
    else:
        try:
            rows = fetch_supabase_rows(sector=args.sector, lookback_days=args.lookback_days)
        except Exception as exc:
            print(f"[backtest] Supabase fetch failed: {exc}", file=sys.stderr)
            print(
                "[backtest] Use --fixtures for offline smoke test, or set Supabase env via Titan config.",
                file=sys.stderr,
            )
            return 1

    if not rows:
        print("[backtest] No rows to evaluate.", file=sys.stderr)
        return 1

    report = run_legacy_vs_v2_ab(
        rows,
        accumulate=bool(args.accumulate),
        flip_guardrail_extra=float(args.flip_guardrail_pct),
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
