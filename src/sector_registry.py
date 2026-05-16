"""Load sector stock lists from Supabase with CSV fallback (NSE and/or BSE)."""

from __future__ import annotations

import os
import csv
from dataclasses import dataclass
from pathlib import Path

from postgrest.exceptions import APIError
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
SECTORS_DIR = ROOT / "data" / "sectors"

# Cap how many instruments we process per run (raise when scaling sector work).
MAX_SYMBOLS = 60

_EXCHANGES = frozenset({"NSE", "BSE"})


@dataclass(frozen=True)
class SectorInstrument:
    """Trading symbol and exchange for Breeze-style APIs (``exchange_code`` NSE/BSE)."""

    symbol: str
    exchange: str

    def __post_init__(self) -> None:
        if self.exchange not in _EXCHANGES:
            raise ValueError(f"exchange must be NSE or BSE, got {self.exchange!r}")


def load_sector_instruments(
    sector_id: str,
    *,
    max_symbols: int | None = None,
) -> list[SectorInstrument]:
    """Load active instruments from Supabase, fallback to ``data/sectors/<sector>.csv``."""
    cap = max_symbols if max_symbols is not None else MAX_SYMBOLS
    if cap < 0:
        raise ValueError("max_symbols must be >= 0")

    sid = sector_id.strip().lower()
    if not sid:
        raise ValueError("sector_id must be non-empty")

    supabase_error: str | None = None
    try:
        rows = _load_sector_instruments_from_supabase(sid)
    except Exception as e:  # noqa: BLE001
        supabase_error = str(e)
        rows = []

    if not rows:
        rows = _load_sector_instruments_from_csv(sid)
        if rows:
            if supabase_error:
                print(f"[SectorRegistry] Supabase load failed; using CSV fallback for {sid!r}: {supabase_error}")
            else:
                print(f"[SectorRegistry] Supabase returned no rows; using CSV fallback for {sid!r}.")
        else:
            if supabase_error:
                raise RuntimeError(
                    f"[SectorRegistry] Supabase load failed and CSV fallback missing/empty for sector {sid!r}: "
                    f"{supabase_error}"
                )
            raise RuntimeError(
                f"[SectorRegistry] No active instruments mapped for sector '{sid}' in Supabase and "
                f"no CSV fallback found at {SECTORS_DIR / f'{sid}.csv'}."
            )

    seen: set[tuple[str, str]] = set()
    ordered: list[SectorInstrument] = []
    for inst in rows:
        key = (inst.symbol, inst.exchange)
        if key not in seen:
            seen.add(key)
            ordered.append(inst)

    return ordered[:cap]


def _load_sector_instruments_from_supabase(sid: str) -> list[SectorInstrument]:
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise ValueError(
            "Missing SUPABASE_URL or SUPABASE_KEY; cannot load sector instruments from Supabase."
        )

    client = create_client(supabase_url, supabase_key)
    try:
        res = (
            client.table("instrument_sector_map")
            .select(
                "is_active,"
                "market_instruments!inner(symbol,exchange,is_active),"
                "sector_catalog!inner(sector_key,is_active)"
            )
            .eq("is_active", True)
            .eq("market_instruments.is_active", True)
            .eq("sector_catalog.is_active", True)
            .eq("sector_catalog.sector_key", sid)
            .execute()
        )
    except APIError as e:
        payload = e.args[0] if e.args else {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        if code == "PGRST205" or "could not find the table" in msg.lower():
            raise RuntimeError(
                "[Supabase] Sector registry tables missing or not exposed (REST). "
                "Run sql/create_sector_registry_tables.sql first. "
                f"PostgREST: {code or 'n/a'} - {msg}"
            ) from e
        raise RuntimeError(f"[Supabase] Sector registry query failed ({code or 'error'}): {msg}") from e

    raw_rows = list((getattr(res, "data", None) or []))
    rows: list[SectorInstrument] = []
    for row in raw_rows:
        inst = row.get("market_instruments") if isinstance(row, dict) else None
        if not isinstance(inst, dict):
            continue
        sym = str(inst.get("symbol", "")).strip().upper()
        exch = str(inst.get("exchange", "")).strip().upper()
        if not sym:
            continue
        if exch not in _EXCHANGES:
            raise ValueError(
                f"Invalid exchange {exch!r} for {sym} in Supabase (use NSE or BSE)"
            )
        rows.append(SectorInstrument(symbol=sym, exchange=exch))
    return rows


def _load_sector_instruments_from_csv(sid: str) -> list[SectorInstrument]:
    path = SECTORS_DIR / f"{sid}.csv"
    if not path.is_file():
        return []
    rows: list[SectorInstrument] = []
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for item in reader:
            sym = str((item or {}).get("symbol", "")).strip().upper()
            exch = str((item or {}).get("exchange", "")).strip().upper()
            if not sym:
                continue
            if exch not in _EXCHANGES:
                raise ValueError(f"Invalid exchange {exch!r} for {sym} in CSV (use NSE or BSE)")
            rows.append(SectorInstrument(symbol=sym, exchange=exch))
    return rows


def load_sector_symbols(sector_id: str, *, max_symbols: int | None = None) -> list[str]:
    """
    Return symbols only (same order as :func:`load_sector_instruments`).

    Prefer :func:`load_sector_instruments` when you need ``exchange`` (e.g. BSE rows).
    """
    return [i.symbol for i in load_sector_instruments(sector_id, max_symbols=max_symbols)]
