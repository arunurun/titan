"""Refresh sector priority rankings and persist daily winners (any sector)."""

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
load_dotenv(ROOT / ".env", override=False)

from config_loader import load_config
from sector_priority import (
    build_sector_rankings,
    persist_daily_winners,
    persist_sector_rankings,
)
from sector_registry import load_sector_instruments


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh rankings + daily winners for a sector")
    p.add_argument("--sector", type=str, required=True, help="sector_catalog.sector_key")
    p.add_argument("--top-n", type=int, default=10, help="Priority / winner count")
    p.add_argument("--max-symbols", type=int, default=None, help="Optional cap while testing")
    args = p.parse_args()
    sector = args.sector.strip().lower()
    top_n = max(1, int(args.top_n))

    cfg = load_config()
    instruments = load_sector_instruments(sector, max_symbols=args.max_symbols)
    rows = build_sector_rankings(
        cfg,
        sector_key=sector,
        instruments=instruments,
        top_n=top_n,
    )
    persist_rank = persist_sector_rankings(cfg, rows)
    persist_winners = persist_daily_winners(cfg, sector_key=sector, top_n=top_n)
    winners = [r for r in rows if r.get("is_priority")]
    out = {
        "sector": sector,
        "rank_rows": len(rows),
        "winners_count": len(winners),
        "winners": [f"{r['symbol']}({r['exchange']})" for r in winners[:top_n]],
        "rank_persist": persist_rank,
        "winner_persist": persist_winners,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
