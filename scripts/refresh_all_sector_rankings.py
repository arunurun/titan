"""Refresh sector priority rankings + daily winners for every active sector (excl. unknown).

Used by the Saturday scheduled workflow. Runs each sector via the same logic as
``refresh_sector_daily_winners.py`` (Breeze-heavy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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

    for sector in sectors:
        proc = subprocess.run(
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
            capture_output=True,
        )
        ok = proc.returncode == 0
        if not ok:
            failed.append(sector)
        out = (proc.stdout or "").strip()
        results.append(
            {
                "sector": sector,
                "ok": ok,
                "exit_code": proc.returncode,
                "stdout_tail": out[-2000:] if out else "",
                "stderr_tail": (proc.stderr or "")[-1000:],
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
