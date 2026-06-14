#!/usr/bin/env python3
"""Read-only diagnostic: how many symbol_daily_features rows carry a `sector` tag that
disagrees with instrument_sector_map (registry). Prints the worst offenders."""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)
from config_loader import load_config
from supabase import create_client


def main() -> int:
    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    # symbol -> set(registry sector_key)
    sym_sectors: dict[str, set[str]] = defaultdict(set)
    off = 0
    while True:
        b = (client.table("instrument_sector_map")
             .select("is_active,market_instruments!inner(symbol,is_active),sector_catalog!inner(sector_key,is_active)")
             .eq("is_active", True).eq("market_instruments.is_active", True)
             .eq("sector_catalog.is_active", True).range(off, off + 999).execute().data or [])
        for r in b:
            mi = r.get("market_instruments") or {}
            sc = r.get("sector_catalog") or {}
            sym = str(mi.get("symbol") or "").strip().upper()
            sk = str(sc.get("sector_key") or "").strip().lower()
            if sym and sk:
                sym_sectors[sym].add(sk)
        if len(b) < 1000:
            break
        off += 1000
    print(f"registry: {len(sym_sectors)} symbols mapped")

    # distinct (symbol, sector) in features
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    off = 0
    while True:
        b = (client.table("symbol_daily_features").select("symbol,sector")
             .order("trade_date").range(off, off + 999).execute().data or [])
        for r in b:
            sym = str(r.get("symbol") or "").strip().upper()
            sec = str(r.get("sector") or "").strip().lower()
            pairs[(sym, sec)] += 1
        if len(b) < 1000:
            break
        off += 1000

    mism = []
    orphan = []  # symbol not in registry at all
    for (sym, sec), cnt in pairs.items():
        reg = sym_sectors.get(sym)
        if not reg:
            orphan.append((sym, sec, cnt))
        elif sec not in reg:
            mism.append((sym, sec, sorted(reg), cnt))

    print(f"\nMISMATCH (feature sector NOT in registry sectors): {len(mism)} symbol/sector pairs")
    for sym, sec, reg, cnt in sorted(mism, key=lambda x: -x[3])[:30]:
        print(f"  {sym:<14} feature={sec:<28} registry={reg} rows={cnt}")
    print(f"\nORPHAN (symbol not in registry): {len(orphan)} pairs (top 15)")
    for sym, sec, cnt in sorted(orphan, key=lambda x: -x[2])[:15]:
        print(f"  {sym:<14} feature={sec:<28} rows={cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
