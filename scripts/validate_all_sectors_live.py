from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
load_dotenv(ROOT / ".env", override=True)

from sector_audit import run_sector_live
from sector_registry import list_active_sector_ids

SUMMARY_RE = re.compile(
    r"Symbols requested:\s*(?P<requested>\d+)\s*\|\s*successful:\s*(?P<successful>\d+)\s*\|\s*"
    r"skipped\(no data\):\s*(?P<skipped>\d+)\s*\|\s*hard failures:\s*(?P<hard>\d+)"
)
HARD_BREAKDOWN_RE = re.compile(r"Hard-failure breakdown:\s*(?P<value>.+)$")


def _parse_digest(digest: str) -> dict[str, object]:
    out: dict[str, object] = {
        "requested": None,
        "successful": None,
        "skipped_no_data": None,
        "hard_failures": None,
        "hard_failure_breakdown": "unknown",
    }
    for line in digest.splitlines():
        m = SUMMARY_RE.search(line)
        if m:
            out["requested"] = int(m.group("requested"))
            out["successful"] = int(m.group("successful"))
            out["skipped_no_data"] = int(m.group("skipped"))
            out["hard_failures"] = int(m.group("hard"))
            continue
        b = HARD_BREAKDOWN_RE.search(line)
        if b:
            out["hard_failure_breakdown"] = b.group("value").strip()
    return out


def main() -> int:
    sectors = list_active_sector_ids(include_unknown=False)
    sectors = [s for s in sectors if s not in {"unknown", "non_equity"}]
    report: dict[str, object] = {
        "run_started_at": datetime.now().isoformat(),
        "sectors_total": len(sectors),
        "sectors": [],
    }
    print(f"[validate] sectors={len(sectors)}")
    for idx, sector in enumerate(sectors, start=1):
        print(f"[validate] ({idx}/{len(sectors)}) running {sector}")
        row: dict[str, object] = {"sector": sector, "status": "ok"}
        try:
            digest = run_sector_live(
                sector,
                digest=True,
                send_email=False,
                max_workers=2,
            )
            row.update(_parse_digest(digest))
        except Exception as exc:  # noqa: BLE001
            row["status"] = "error"
            row["error"] = str(exc)
            row["traceback_tail"] = traceback.format_exc().strip().splitlines()[-1]
        report["sectors"].append(row)

    report["run_finished_at"] = datetime.now().isoformat()
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "live_sector_validation.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[validate] report={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
