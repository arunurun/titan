"""Refresh AI sector rankings and persist top daily winners."""

from __future__ import annotations

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
    cfg = load_config()
    sector = "ai"
    top_n = 10
    instruments = load_sector_instruments(sector)
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
        "winners": [f"{r['symbol']}({r['exchange']})" for r in winners],
        "rank_persist": persist_rank,
        "winner_persist": persist_winners,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

