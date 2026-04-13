from __future__ import annotations

import os
from collections.abc import Iterable

from supabase import Client, create_client

from src.classification.sector_resolver import resolve_sector_key
from src.models import UniverseInstrument, normalize_sector_key


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def make_client() -> Client:
    return create_client(_require_env("SUPABASE_URL"), _require_env("SUPABASE_SERVICE_ROLE_KEY"))


def _get_or_create_sector_id(client: Client, sector_key: str) -> str:
    key = normalize_sector_key(sector_key)
    client.table("sector_catalog").upsert(
        {
            "sector_key": key,
            "sector_name": key.replace("_", " ").title(),
            "is_active": True,
        },
        on_conflict="sector_key",
    ).execute()
    row = (
        client.table("sector_catalog")
        .select("id")
        .eq("sector_key", key)
        .limit(1)
        .execute()
        .data
    )
    if not row:
        raise RuntimeError(f"Could not resolve sector id for {key}")
    return row[0]["id"]


def _get_or_create_instrument_id(client: Client, inst: UniverseInstrument) -> str:
    client.table("market_instruments").upsert(
        {
            "exchange": inst.exchange,
            "symbol": inst.symbol,
            "instrument_name": inst.instrument_name or None,
            "isin": inst.isin or None,
            "is_active": True,
        },
        on_conflict="exchange,symbol",
    ).execute()
    row = (
        client.table("market_instruments")
        .select("id")
        .eq("exchange", inst.exchange)
        .eq("symbol", inst.symbol)
        .limit(1)
        .execute()
        .data
    )
    if not row:
        raise RuntimeError(f"Could not resolve instrument id for {inst.exchange}:{inst.symbol}")
    return row[0]["id"]


def _load_overrides(client: Client) -> dict[tuple[str, str], str]:
    rows = (
        client.table("sector_overrides")
        .select("exchange,symbol,sector_key")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    out: dict[tuple[str, str], str] = {}
    for row in rows:
        ex = str(row.get("exchange", "")).strip().upper()
        sym = str(row.get("symbol", "")).strip().upper()
        sector_key = normalize_sector_key(str(row.get("sector_key", "")))
        if ex in {"NSE", "BSE"} and sym:
            out[(ex, sym)] = sector_key
    return out


def _insert_run_start(client: Client) -> str:
    result = client.table("scanner_runs").insert({"status": "running"}).execute().data or []
    if not result:
        raise RuntimeError("Failed to create scanner run row")
    return result[0]["id"]


def _finish_run(client: Client, run_id: str, *, status: str, message: str, counters: dict[str, int]) -> None:
    client.table("scanner_runs").update(
        {
            "status": status,
            "message": message,
            "completed_at": "now()",
            "total_seen": counters.get("total_seen", 0),
            "inserted_count": counters.get("inserted_count", 0),
            "updated_count": counters.get("updated_count", 0),
            "deactivated_count": counters.get("deactivated_count", 0),
        }
    ).eq("id", run_id).execute()


def sync_universe(client: Client, instruments: Iterable[UniverseInstrument]) -> dict[str, int]:
    run_id = _insert_run_start(client)
    counters = {"total_seen": 0, "inserted_count": 0, "updated_count": 0, "deactivated_count": 0}
    try:
        override_map = _load_overrides(client)
        seen_keys: set[tuple[str, str]] = set()
        unknown_id = _get_or_create_sector_id(client, "unknown")

        for inst in instruments:
            key = (inst.exchange, inst.symbol)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            counters["total_seen"] += 1

            instrument_id = _get_or_create_instrument_id(client, inst)
            sector_key, source = resolve_sector_key(inst, override_map)
            sector_id = unknown_id if sector_key == "unknown" else _get_or_create_sector_id(client, sector_key)

            client.table("instrument_sector_map").upsert(
                {
                    "instrument_id": instrument_id,
                    "sector_id": sector_id,
                    "source": source,
                    "is_active": True,
                },
                on_conflict="instrument_id,sector_id",
            ).execute()
            counters["updated_count"] += 1

        active_rows = (
            client.table("market_instruments")
            .select("exchange,symbol")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        stale = []
        for row in active_rows:
            key = (str(row.get("exchange", "")).strip().upper(), str(row.get("symbol", "")).strip().upper())
            if key not in seen_keys:
                stale.append(key)
        for ex, sym in stale:
            client.table("market_instruments").update({"is_active": False}).eq("exchange", ex).eq(
                "symbol", sym
            ).execute()
            counters["deactivated_count"] += 1

        _finish_run(client, run_id, status="completed", message="weekly sync completed", counters=counters)
        return counters
    except Exception as exc:
        _finish_run(client, run_id, status="failed", message=str(exc), counters=counters)
        raise
