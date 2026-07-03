from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from supabase import Client, create_client

from src.classification.sector_resolver import resolve_sector_key
from src.models import UniverseInstrument, normalize_sector_key

logger = logging.getLogger("provider-universe-sync.supabase")
CHUNK_SIZE = 400
MAX_RETRIES = 5


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def make_client() -> Client:
    supabase_url = _require_env("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not key:
        key = _require_env("SUPABASE_KEY")
        logger.warning("SUPABASE_SERVICE_ROLE_KEY missing; falling back to SUPABASE_KEY.")
    return create_client(supabase_url, key)


def _chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    needles = (
        "connectionterminated",
        "connection reset",
        "timed out",
        "timeout",
        "too many requests",
        "server disconnected",
        "stream",
        "502",
        "503",
        "504",
    )
    return any(x in msg for x in needles)


def _run_with_retry(op_name: str, fn):
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - network behavior
            last_exc = exc
            if attempt >= MAX_RETRIES or not _is_retryable(exc):
                raise
            logger.warning(
                "%s failed (attempt %s/%s): %s; retrying in %.1fs",
                op_name,
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2.0, 15.0)
    if last_exc is not None:
        raise last_exc


def _load_overrides(client: Client) -> dict[tuple[str, str], str]:
    rows = _run_with_retry(
        "load sector_overrides",
        lambda: (
            client.table("sector_overrides")
            .select("exchange,symbol,sector_key")
            .eq("is_active", True)
            .execute()
            .data
            or []
        ),
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
    result = _run_with_retry(
        "insert scanner_runs(start)",
        lambda: client.table("scanner_runs").insert({"status": "running"}).execute().data or [],
    )
    if not result:
        raise RuntimeError("Failed to create scanner run row")
    return result[0]["id"]


def _finish_run(client: Client, run_id: str, *, status: str, message: str, counters: dict[str, int]) -> None:
    _run_with_retry(
        "update scanner_runs(end)",
        lambda: client.table("scanner_runs")
        .update(
            {
                "status": status,
                "message": message,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "total_seen": counters.get("total_seen", 0),
                "inserted_count": counters.get("inserted_count", 0),
                "updated_count": counters.get("updated_count", 0),
                "deactivated_count": counters.get("deactivated_count", 0),
            }
        )
        .eq("id", run_id)
        .execute(),
    )


def _upsert_sector_catalog(client: Client, sector_keys: set[str]) -> dict[str, str]:
    rows = [
        {"sector_key": k, "sector_name": k.replace("_", " ").title(), "is_active": True}
        for k in sorted(sector_keys)
    ]
    for chunk in _chunked(rows, CHUNK_SIZE):
        _run_with_retry(
            "upsert sector_catalog",
            lambda chunk=chunk: client.table("sector_catalog")
            .upsert(chunk, on_conflict="sector_key")
            .execute(),
        )
    id_map: dict[str, str] = {}
    keys = sorted(sector_keys)
    for chunk in _chunked(keys, CHUNK_SIZE):
        data = _run_with_retry(
            "select sector_catalog ids",
            lambda chunk=chunk: client.table("sector_catalog")
            .select("id,sector_key")
            .in_("sector_key", chunk)
            .execute()
            .data
            or [],
        )
        for row in data:
            key = normalize_sector_key(str(row.get("sector_key", "")))
            sid = str(row.get("id", "")).strip()
            if key and sid:
                id_map[key] = sid
    return id_map


def _upsert_market_instruments(client: Client, instruments: list[UniverseInstrument]) -> None:
    rows = [
        {
            "exchange": inst.exchange,
            "symbol": inst.symbol,
            "instrument_name": inst.instrument_name or None,
            "isin": inst.isin or None,
            "breeze_stock_code": inst.breeze_stock_code or None,
            "is_active": True,
        }
        for inst in instruments
    ]
    for chunk in _chunked(rows, CHUNK_SIZE):
        _run_with_retry(
            "upsert market_instruments",
            lambda chunk=chunk: client.table("market_instruments")
            .upsert(chunk, on_conflict="exchange,symbol")
            .execute(),
        )


def _fetch_instrument_ids(client: Client, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    by_exchange: dict[str, list[str]] = {"NSE": [], "BSE": []}
    for ex, sym in sorted(keys):
        if ex in by_exchange:
            by_exchange[ex].append(sym)

    for exchange, symbols in by_exchange.items():
        for chunk in _chunked(symbols, CHUNK_SIZE):
            data = _run_with_retry(
                "select market_instruments ids",
                lambda exchange=exchange, chunk=chunk: client.table("market_instruments")
                .select("id,exchange,symbol")
                .eq("exchange", exchange)
                .in_("symbol", chunk)
                .execute()
                .data
                or [],
            )
            for row in data:
                ex = str(row.get("exchange", "")).strip().upper()
                sym = str(row.get("symbol", "")).strip().upper()
                iid = str(row.get("id", "")).strip()
                if ex in {"NSE", "BSE"} and sym and iid:
                    result[(ex, sym)] = iid
    return result


def _upsert_sector_mappings(client: Client, mapping_rows: list[dict[str, str | bool]]) -> None:
    for chunk in _chunked(mapping_rows, CHUNK_SIZE):
        _run_with_retry(
            "upsert instrument_sector_map",
            lambda chunk=chunk: client.table("instrument_sector_map")
            .upsert(chunk, on_conflict="instrument_id,sector_id")
            .execute(),
        )


def _deactivate_stale_instruments(client: Client, seen_keys: set[tuple[str, str]]) -> int:
    active = _run_with_retry(
        "select active market_instruments",
        lambda: client.table("market_instruments")
        .select("id,exchange,symbol")
        .eq("is_active", True)
        .in_("exchange", ["NSE", "BSE"])
        .execute()
        .data
        or [],
    )
    stale_ids: list[str] = []
    for row in active:
        ex = str(row.get("exchange", "")).strip().upper()
        sym = str(row.get("symbol", "")).strip().upper()
        iid = str(row.get("id", "")).strip()
        if iid and (ex, sym) not in seen_keys:
            stale_ids.append(iid)

    if not stale_ids:
        return 0

    for chunk in _chunked(stale_ids, CHUNK_SIZE):
        _run_with_retry(
            "deactivate market_instruments chunk",
            lambda chunk=chunk: client.table("market_instruments")
            .update({"is_active": False})
            .in_("id", chunk)
            .execute(),
        )
        _run_with_retry(
            "deactivate instrument_sector_map chunk",
            lambda chunk=chunk: client.table("instrument_sector_map")
            .update({"is_active": False})
            .in_("instrument_id", chunk)
            .execute(),
        )
    return len(stale_ids)


def sync_universe(client: Client, instruments: Iterable[UniverseInstrument]) -> dict[str, int]:
    run_id = _insert_run_start(client)
    counters = {"total_seen": 0, "inserted_count": 0, "updated_count": 0, "deactivated_count": 0}
    try:
        override_map = _load_overrides(client)
        unique: dict[tuple[str, str], UniverseInstrument] = {}
        for inst in instruments:
            ex = inst.exchange.strip().upper()
            sym = inst.symbol.strip().upper()
            if ex not in {"NSE", "BSE"} or not sym:
                continue
            unique.setdefault(
                (ex, sym),
                UniverseInstrument(
                    exchange=ex,
                    symbol=sym,
                    instrument_name=inst.instrument_name,
                    isin=inst.isin,
                    official_sector_key=inst.official_sector_key,
                    official_industry=inst.official_industry,
                    breeze_stock_code=inst.breeze_stock_code,
                ),
            )

        if not unique:
            raise RuntimeError("No valid NSE/BSE instruments found to sync.")

        seen_keys = set(unique.keys())
        counters["total_seen"] = len(seen_keys)

        sector_assignment: dict[tuple[str, str], tuple[str, str]] = {}
        sector_keys: set[str] = {"unknown"}
        for key, inst in unique.items():
            sector_key, source = resolve_sector_key(inst, override_map)
            normalized = normalize_sector_key(sector_key)
            sector_assignment[key] = (normalized, source)
            sector_keys.add(normalized)

        logger.info("Syncing %s instruments across %s sectors", len(unique), len(sector_keys))
        sector_ids = _upsert_sector_catalog(client, sector_keys)
        _upsert_market_instruments(client, list(unique.values()))
        instrument_ids = _fetch_instrument_ids(client, seen_keys)

        mapping_rows: list[dict[str, str | bool]] = []
        for key in sorted(seen_keys):
            instrument_id = instrument_ids.get(key)
            if not instrument_id:
                continue
            sector_key, source = sector_assignment[key]
            sector_id = sector_ids.get(sector_key) or sector_ids.get("unknown")
            if not sector_id:
                raise RuntimeError("Missing sector id for unknown sector")
            mapping_rows.append(
                {
                    "instrument_id": instrument_id,
                    "sector_id": sector_id,
                    "source": source,
                    "is_active": True,
                }
            )

        _upsert_sector_mappings(client, mapping_rows)
        counters["updated_count"] = len(mapping_rows)
        counters["deactivated_count"] = _deactivate_stale_instruments(client, seen_keys)

        _finish_run(client, run_id, status="completed", message="weekly sync completed", counters=counters)
        return counters
    except Exception as exc:
        _finish_run(client, run_id, status="failed", message=str(exc), counters=counters)
        raise
