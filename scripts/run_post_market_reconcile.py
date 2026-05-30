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
    sector: str,
    max_symbols: int | None,
    workers: int | None,
) -> int:
    cmd = [sys.executable, "main.py", "--sector", sector, "--sector-digest"]
    if max_symbols is not None:
        cmd.extend(["--sector-max-symbols", str(max(1, int(max_symbols)))])
    if workers is not None:
        cmd.extend(["--sector-workers", str(max(1, int(workers)))])
    env = dict(os.environ)
    env["TITAN_ENABLE_ANALYSIS_STORE"] = env.get("TITAN_ENABLE_ANALYSIS_STORE", "1")
    print(f"[reconcile] running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run post-market stock-level reconcile and optional backfill persistence."
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

    sector = args.sector.strip().lower()
    if not sector:
        raise ValueError("sector must be a non-empty string")

    if not args.backfill_only:
        rc = _run_live_reconcile(
            sector=sector,
            max_symbols=args.max_symbols,
            workers=args.workers,
        )
        if rc != 0:
            return rc

    if int(args.backfill_days or 0) > 0:
        cfg = load_config()
        out = persist_reconcile_backfill(
            cfg,
            sector=sector,
            days=max(1, int(args.backfill_days)),
        )
        print(
            "[reconcile] backfill persisted="
            f"{out.get('persisted', 0)} days={out.get('days', 0)} sector={sector}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
