"""Refresh sector priority rankings + daily winners for every active sector (excl. unknown).

Used by the Saturday scheduled workflow. Runs each sector via the same logic as
``refresh_sector_daily_winners.py`` (Breeze-heavy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_SECONDS = 60.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh rankings for all sectors")
    p.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Winners / priority depth per sector (default 10)",
    )
    p.add_argument(
        "--exclude-sectors",
        type=str,
        default="unknown,non_equity",
        help="Comma-separated sector_key values to skip",
    )
    args = p.parse_args()
    top_n = max(1, int(args.top_n))
    excl = {x.strip().lower() for x in str(args.exclude_sectors).split(",") if x.strip()}

    sys.path.insert(0, str(ROOT / "src"))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    from sector_registry import list_active_sector_ids

    sectors = [s for s in list_active_sector_ids(include_unknown=False) if s not in excl]
    sectors.sort()
    if not sectors:
        print(json.dumps({"error": "no_sectors", "exclude": sorted(excl)}, indent=2))
        return 1

    script = ROOT / "scripts" / "refresh_sector_daily_winners.py"
    results: list[dict[str, object]] = []
    failed: list[str] = []

    for idx, sector in enumerate(sectors, start=1):
        sector_started = time.perf_counter()
        print(
            f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] sector={sector} started top_n={top_n}",
            flush=True,
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--sector",
                sector,
                "--top-n",
                str(top_n),
            ],
            cwd=ROOT,
            text=True,
        )
        next_heartbeat = time.perf_counter() + HEARTBEAT_SECONDS
        while proc.poll() is None:
            now = time.perf_counter()
            if now >= next_heartbeat:
                elapsed = now - sector_started
                print(
                    f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] heartbeat sector={sector} elapsed_s={elapsed:.1f}",
                    flush=True,
                )
                next_heartbeat = now + HEARTBEAT_SECONDS
            time.sleep(1.0)
        exit_code = int(proc.returncode or 0)
        ok = exit_code == 0
        if not ok:
            failed.append(sector)
        elapsed_total = time.perf_counter() - sector_started
        print(
            f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] sector={sector} finished exit_code={exit_code} elapsed_s={elapsed_total:.1f}",
            flush=True,
        )
        results.append(
            {
                "sector": sector,
                "ok": ok,
                "exit_code": exit_code,
                "elapsed_seconds": round(elapsed_total, 2),
            }
        )

    summary = {
        "top_n": top_n,
        "exclude": sorted(excl),
        "sector_count": len(sectors),
        "failed": sorted(failed),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
