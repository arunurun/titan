"""Refresh persisted sector priority rankings (pilot default: ai)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from sector_priority import build_sector_rankings, persist_sector_rankings
from sector_registry import load_sector_instruments


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh sector priority rankings")
    p.add_argument("--sector", type=str, default="ai", help="Sector id to rank (default: ai)")
    p.add_argument("--top-n", type=int, default=10, help="Number of priority symbols to flag")
    p.add_argument("--max-symbols", type=int, default=None, help="Optional cap while testing")
    args = p.parse_args()

    cfg = load_config()
    sector = args.sector.strip().lower()
    instruments = load_sector_instruments(sector, max_symbols=args.max_symbols)
    rows = build_sector_rankings(
        cfg,
        sector_key=sector,
        instruments=instruments,
        top_n=max(1, int(args.top_n)),
    )
    persisted = persist_sector_rankings(cfg, rows)
    top = [r for r in rows if r.get("is_priority")]
    issue_counts: dict[str, int] = {}
    for r in rows:
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        issues = meta.get("issues") if isinstance(meta.get("issues"), list) else []
        for item in issues:
            k = str(item).strip()
            if not k:
                continue
            issue_counts[k] = issue_counts.get(k, 0) + 1
    issue_samples = [
        {
            "symbol": r["symbol"],
            "exchange": r["exchange"],
            "issues": (r.get("meta") or {}).get("issues", []),
            "market_cap_source": (r.get("meta") or {}).get("market_cap_source"),
            "rows_count": (r.get("meta") or {}).get("rows_count"),
        }
        for r in rows
        if (r.get("meta") or {}).get("issues")
    ][:12]
    out = {
        "sector": sector,
        "rows": len(rows),
        "priority_count": len(top),
        "top_symbols": [f"{r['symbol']}({r['exchange']})" for r in top[: args.top_n]],
        "persist": persisted,
        "issue_summary": issue_counts,
        "issue_samples": issue_samples,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

