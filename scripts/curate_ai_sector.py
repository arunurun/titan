"""Curate AI sector membership using research-backed strict allowlist."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
IST = ZoneInfo("Asia/Kolkata")

STRICT_AI_ALLOWLIST = {
    # Final strict set (10 active + 2 backups) from deep validation.
    "E2E",
    "SUBEXLTD",
    "GENESYS",
    "MOSCHIP",
    "HAPPSTMNDS",
    "DATAMATICS",
    "TANLA",
    "NETWEB",
    "PERSISTENT",
    "AFFLE",
    # Backups
    "KPITTECH",
    "TATAELXSI",
}


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL/SUPABASE_KEY")
    return create_client(url, key)


def main() -> int:
    client = _client()
    now = datetime.now(IST).isoformat(timespec="seconds")
    # Resolve sector ids.
    sec = (
        client.table("sector_catalog")
        .select("id,sector_key")
        .in_("sector_key", ["ai", "unknown"])
        .execute()
        .data
        or []
    )
    by_key = {str(r.get("sector_key", "")).strip().lower(): str(r.get("id", "")).strip() for r in sec}
    ai_id = by_key.get("ai")
    unknown_id = by_key.get("unknown")
    if not ai_id or not unknown_id:
        raise RuntimeError("Missing ai/unknown sector ids in sector_catalog")

    # Active AI mapped instruments.
    ai_maps = (
        client.table("instrument_sector_map")
        .select("id,instrument_id,market_instruments!inner(id,symbol,exchange)")
        .eq("sector_id", ai_id)
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    # Universe rows that should be force-included in AI.
    allow_rows = (
        client.table("market_instruments")
        .select("id,symbol,exchange")
        .in_("symbol", sorted(STRICT_AI_ALLOWLIST))
        .in_("exchange", ["NSE", "BSE"])
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    # NSE-first canonicalization: one active mapping per symbol.
    by_symbol: dict[str, dict] = {}
    for row in allow_rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        ex = str(row.get("exchange", "")).strip().upper()
        iid = str(row.get("id", "")).strip()
        if not sym or ex not in ("NSE", "BSE") or not iid:
            continue
        prev = by_symbol.get(sym)
        if prev is None:
            by_symbol[sym] = {"id": iid, "symbol": sym, "exchange": ex}
            continue
        # Prefer NSE over BSE for active AI mapping.
        if prev["exchange"] != "NSE" and ex == "NSE":
            by_symbol[sym] = {"id": iid, "symbol": sym, "exchange": ex}
    allow_ids = [v["id"] for v in by_symbol.values()]
    allow_pairs = [(v["symbol"], v["exchange"]) for v in by_symbol.values()]
    found_allow_syms = {sym for sym, _ex in allow_pairs}
    unresolved_allow_syms = sorted(STRICT_AI_ALLOWLIST - found_allow_syms)
    inst_rows: list[dict] = []
    for row in ai_maps:
        mi = row.get("market_instruments") if isinstance(row, dict) else None
        if not isinstance(mi, dict):
            continue
        symbol = str(mi.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        if symbol in STRICT_AI_ALLOWLIST:
            continue
        inst_rows.append(
            {
                "id": str(mi.get("id", "")).strip(),
                "symbol": symbol,
                "exchange": str(mi.get("exchange", "")).strip().upper(),
            }
        )
    instrument_ids = [r["id"] for r in inst_rows if r.get("id")]
    symbol_pairs = [
        (str(r.get("symbol", "")).strip().upper(), str(r.get("exchange", "")).strip().upper())
        for r in inst_rows
        if str(r.get("symbol", "")).strip()
    ]
    if not instrument_ids:
        print(json.dumps({"updated": False, "reason": "no_matching_instruments"}))
        return 0

    # Deactivate active AI mappings for those instruments.
    deactivated = (
        client.table("instrument_sector_map")
        .update({"is_active": False, "updated_at": now})
        .eq("sector_id", ai_id)
        .eq("is_active", True)
        .in_("instrument_id", instrument_ids)
        .execute()
        .data
        or []
    )

    # Upsert unknown mapping so symbols still map somewhere, and mark override.
    unknown_map_rows = [
        {
            "instrument_id": iid,
            "sector_id": unknown_id,
            "source": "override",
            "is_active": True,
            "updated_at": now,
        }
        for iid in instrument_ids
    ]
    client.table("instrument_sector_map").upsert(
        unknown_map_rows,
        on_conflict="instrument_id,sector_id",
    ).execute()
    # Activate AI mapping for strict allowlist rows.
    ai_map_rows = [
        {
            "instrument_id": iid,
            "sector_id": ai_id,
            "source": "override",
            "is_active": True,
            "updated_at": now,
        }
        for iid in allow_ids
    ]
    if ai_map_rows:
        client.table("instrument_sector_map").upsert(
            ai_map_rows,
            on_conflict="instrument_id,sector_id",
        ).execute()

    override_rows = [
        {
            "exchange": ex,
            "symbol": sym,
            "sector_key": "unknown",
            "reason": json.dumps(
                {
                    "policy": "ai_curation_v1",
                    "decision": "exclude_from_ai",
                    "timestamp": now,
                }
            ),
            "is_active": True,
            "updated_at": now,
        }
        for sym, ex in symbol_pairs
    ]
    client.table("sector_overrides").upsert(
        override_rows,
        on_conflict="exchange,symbol",
    ).execute()
    # Mark strict allowlist symbols as AI overrides.
    include_override_rows = [
        {
            "exchange": ex,
            "symbol": sym,
            "sector_key": "ai",
            "reason": json.dumps(
                {
                    "policy": "ai_curation_v2_deepscan",
                    "decision": "include_in_ai",
                    "timestamp": now,
                }
            ),
            "is_active": True,
            "updated_at": now,
        }
        for sym, ex in allow_pairs
    ]
    if include_override_rows:
        client.table("sector_overrides").upsert(
            include_override_rows,
            on_conflict="exchange,symbol",
        ).execute()

    out = {
        "updated": True,
        "allowlist_size": len(STRICT_AI_ALLOWLIST),
        "allowlist_found_symbols": sorted(found_allow_syms),
        "allowlist_active_pairs": sorted(f"{sym}({ex})" for sym, ex in allow_pairs),
        "allowlist_unresolved_symbols": unresolved_allow_syms,
        "symbols_removed_from_ai": sorted({r["symbol"] for r in inst_rows}),
        "instrument_rows": len(instrument_ids),
        "ai_rows_deactivated": len(deactivated),
        "override_rows_upserted": len(override_rows),
        "ai_override_rows_upserted": len(include_override_rows),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

