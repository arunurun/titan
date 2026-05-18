"""Strict sector membership from JSON allowlist (same pattern as scripts/curate_ai_sector.py)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env", override=False)

IST = ZoneInfo("Asia/Kolkata")

# PostgREST URL/query limits: large `.in_(instrument_id, ...)` and huge upserts return 400.
_BATCH_IN = 50
_BATCH_UPSERT = 150


def _chunks(xs: list[Any], n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL/SUPABASE_KEY")
    return create_client(url, key)


def _load_allowlist(path: Path) -> tuple[str, str, set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sector_key = str(data.get("sector_key", "")).strip().lower()
    policy = str(data.get("policy", "sector_strict_allowlist_v1")).strip()
    symbols = {str(s).strip().upper() for s in (data.get("symbols") or []) if str(s).strip()}
    return sector_key, policy, symbols


def main() -> int:
    p = argparse.ArgumentParser(description="Curate sector to strict JSON allowlist")
    p.add_argument("--sector-key", type=str, required=True, help="sector_catalog.sector_key")
    p.add_argument(
        "--allowlist",
        type=str,
        default="",
        help="Path to JSON allowlist (default: data/sector_allowlists/<sector-key>.json)",
    )
    args = p.parse_args()
    sector_key = args.sector_key.strip().lower()
    allow_path = Path(args.allowlist) if str(args.allowlist).strip() else ROOT / "data" / "sector_allowlists" / f"{sector_key}.json"
    if not allow_path.is_file():
        raise SystemExit(f"Allowlist not found: {allow_path}")

    file_sector, policy, allow_syms = _load_allowlist(allow_path)
    if file_sector != sector_key:
        raise SystemExit(f"sector_key mismatch: --sector-key={sector_key!r} file={file_sector!r}")

    client = _client()
    now = datetime.now(IST).isoformat(timespec="seconds")

    sec = (
        client.table("sector_catalog")
        .select("id,sector_key")
        .in_("sector_key", [sector_key, "unknown"])
        .execute()
        .data
        or []
    )
    by_key = {str(r.get("sector_key", "")).strip().lower(): str(r.get("id", "")).strip() for r in sec}
    target_id = by_key.get(sector_key)
    unknown_id = by_key.get("unknown")
    if not target_id or not unknown_id:
        raise RuntimeError(f"Missing sector ids in sector_catalog for {sector_key!r} and/or unknown")

    allow_rows = (
        client.table("market_instruments")
        .select("id,symbol,exchange")
        .in_("symbol", sorted(allow_syms))
        .in_("exchange", ["NSE", "BSE"])
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
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
        if prev["exchange"] != "NSE" and ex == "NSE":
            by_symbol[sym] = {"id": iid, "symbol": sym, "exchange": ex}
    allow_ids = {v["id"] for v in by_symbol.values()}
    allow_pairs = [(v["symbol"], v["exchange"]) for v in by_symbol.values()]
    found_syms = {sym for sym, _ex in allow_pairs}
    unresolved = sorted(allow_syms - found_syms)

    active_maps = (
        client.table("instrument_sector_map")
        .select("id,instrument_id,market_instruments!inner(id,symbol,exchange)")
        .eq("sector_id", target_id)
        .eq("is_active", True)
        .execute()
        .data
        or []
    )

    duplicate_map_ids: list[str] = []
    for row in active_maps:
        mi = row.get("market_instruments") if isinstance(row, dict) else None
        if not isinstance(mi, dict):
            continue
        sym = str(mi.get("symbol", "")).strip().upper()
        iid = str(mi.get("id", "")).strip()
        if sym in allow_syms and iid and iid not in allow_ids:
            map_id = str(row.get("id", "")).strip()
            if map_id:
                duplicate_map_ids.append(map_id)

    if duplicate_map_ids:
        for chunk in _chunks(duplicate_map_ids, _BATCH_IN):
            client.table("instrument_sector_map").update({"is_active": False, "updated_at": now}).in_(
                "id", chunk
            ).execute()

    inst_rows: list[dict] = []
    for row in active_maps:
        mi = row.get("market_instruments") if isinstance(row, dict) else None
        if not isinstance(mi, dict):
            continue
        symbol = str(mi.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        iid = str(mi.get("id", "")).strip()
        if symbol in allow_syms and iid in allow_ids:
            continue
        inst_rows.append(
            {
                "id": iid,
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

    if not allow_syms:
        print(json.dumps({"updated": False, "reason": "allowlist_empty"}, indent=2))
        return 1
    if not allow_ids:
        print(
            json.dumps(
                {
                    "updated": False,
                    "reason": "allowlist_unresolved",
                    "unresolved_allowlist": unresolved,
                },
                indent=2,
            )
        )
        return 1

    deactivated: list[Any] = []
    if instrument_ids:
        for chunk in _chunks(instrument_ids, _BATCH_IN):
            res = (
                client.table("instrument_sector_map")
                .update({"is_active": False, "updated_at": now})
                .eq("sector_id", target_id)
                .eq("is_active", True)
                .in_("instrument_id", chunk)
                .execute()
            )
            deactivated.extend(list(getattr(res, "data", None) or []))

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
    if unknown_map_rows:
        for chunk in _chunks(unknown_map_rows, _BATCH_UPSERT):
            client.table("instrument_sector_map").upsert(
                chunk,
                on_conflict="instrument_id,sector_id",
            ).execute()

    target_map_rows = [
        {
            "instrument_id": iid,
            "sector_id": target_id,
            "source": "override",
            "is_active": True,
            "updated_at": now,
        }
        for iid in sorted(allow_ids)
    ]
    if target_map_rows:
        for chunk in _chunks(target_map_rows, _BATCH_UPSERT):
            client.table("instrument_sector_map").upsert(
                chunk,
                on_conflict="instrument_id,sector_id",
            ).execute()

    override_rows = [
        {
            "exchange": ex,
            "symbol": sym,
            "sector_key": "unknown",
            "reason": json.dumps(
                {
                    "policy": policy,
                    "decision": f"exclude_from_{sector_key}",
                    "target_sector": sector_key,
                    "timestamp": now,
                }
            ),
            "is_active": True,
            "updated_at": now,
        }
        for sym, ex in symbol_pairs
    ]
    if override_rows:
        for chunk in _chunks(override_rows, _BATCH_UPSERT):
            client.table("sector_overrides").upsert(
                chunk,
                on_conflict="exchange,symbol",
            ).execute()

    include_override_rows = [
        {
            "exchange": ex,
            "symbol": sym,
            "sector_key": sector_key,
            "reason": json.dumps(
                {
                    "policy": policy,
                    "decision": f"include_in_{sector_key}",
                    "timestamp": now,
                }
            ),
            "is_active": True,
            "updated_at": now,
        }
        for sym, ex in allow_pairs
    ]
    if include_override_rows:
        for chunk in _chunks(include_override_rows, _BATCH_UPSERT):
            client.table("sector_overrides").upsert(
                chunk,
                on_conflict="exchange,symbol",
            ).execute()

    out = {
        "updated": True,
        "sector_key": sector_key,
        "allowlist_path": str(allow_path.as_posix()),
        "allowlist_declared": len(allow_syms),
        "allowlist_resolved_symbols": sorted(found_syms),
        "allowlist_unresolved_symbols": unresolved,
        "allowlist_active_pairs": sorted(f"{sym}({ex})" for sym, ex in allow_pairs),
        "symbols_removed_from_sector": sorted({r["symbol"] for r in inst_rows}),
        "instrument_rows_deactivated": len(instrument_ids),
        "sector_rows_deactivated": len(deactivated),
        "duplicate_exchange_maps_cleared": len(duplicate_map_ids),
        "override_rows_upserted": len(override_rows),
        "include_override_rows_upserted": len(include_override_rows),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
