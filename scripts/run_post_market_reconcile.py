from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config_loader import load_config
from analysis_store import persist_reconcile_backfill


def _run_live_reconcile(
    *,
    sector: str | None,
    all_stocks: bool,
    max_symbols: int | None,
    workers: int | None,
) -> int:
    if all_stocks:
        cmd = [sys.executable, "main.py", "--all-sectors", "--all-sector-workers", "1"]
    else:
        cmd = [sys.executable, "main.py", "--sector", str(sector or "").strip().lower(), "--sector-digest"]
    if max_symbols is not None:
        cmd.extend(["--sector-max-symbols", str(max(1, int(max_symbols)))])
    if workers is not None:
        cmd.extend(["--sector-workers", str(max(1, int(workers)))])
    env = dict(os.environ)
    env["TITAN_ENABLE_ANALYSIS_STORE"] = env.get("TITAN_ENABLE_ANALYSIS_STORE", "1")
    env["TITAN_RECONCILE_REPORT_ONLY"] = env.get("TITAN_RECONCILE_REPORT_ONLY", "1")
    if all_stocks:
        env["TITAN_ALL_SECTORS_SINGLE_DIGEST"] = env.get("TITAN_ALL_SECTORS_SINGLE_DIGEST", "1")
    print(f"[reconcile] running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run post-market stock-level reconcile and optional backfill persistence."
    )
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
        help="Skip live sector run and only persist backfill reconcile summaries.",
    )
    args = parser.parse_args()

    all_stocks_scope = str(args.scope).strip().lower() == "all-stocks"
    sector = args.sector.strip().lower()
    if not all_stocks_scope and not sector:
        raise ValueError("sector must be a non-empty string when --scope=sector")

    if not args.backfill_only:
        rc = _run_live_reconcile(
            sector=sector if not all_stocks_scope else None,
            all_stocks=all_stocks_scope,
            max_symbols=args.max_symbols,
            workers=args.workers,
        )
        if rc != 0:
            return rc

    if int(args.backfill_days or 0) > 0:
        cfg = load_config()
        out = persist_reconcile_backfill(
            cfg,
            sector=(sector if not all_stocks_scope else None),
            all_stocks=all_stocks_scope,
            days=max(1, int(args.backfill_days)),
        )
        print(
            "[reconcile] backfill persisted="
            f"{out.get('persisted', 0)} days={out.get('days', 0)} "
            f"scope={'all-stocks' if all_stocks_scope else f'sector:{sector}'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
