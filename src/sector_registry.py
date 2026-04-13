"""Load sector stock lists from Supabase sector registry tables (NSE and/or BSE)."""

from __future__ import annotations

import os
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
    """
    Read active instruments for ``sector_id`` from Supabase tables:
    ``sector_catalog`` + ``instrument_sector_map`` + ``market_instruments``.

    When ``max_symbols`` is None, uses :data:`MAX_SYMBOLS`.
    """
    cap = max_symbols if max_symbols is not None else MAX_SYMBOLS
    if cap < 0:
        raise ValueError("max_symbols must be >= 0")

    sid = sector_id.strip().lower()
    if not sid:
        raise ValueError("sector_id must be non-empty")

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
    if not raw_rows:
        raise RuntimeError(
            f"[SectorRegistry] No active instruments mapped for sector '{sid}' in Supabase."
        )

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

    seen: set[tuple[str, str]] = set()
    ordered: list[SectorInstrument] = []
    for inst in rows:
        key = (inst.symbol, inst.exchange)
        if key not in seen:
            seen.add(key)
            ordered.append(inst)

    return ordered[:cap]


def load_sector_symbols(sector_id: str, *, max_symbols: int | None = None) -> list[str]:
    """
    Return symbols only (same order as :func:`load_sector_instruments`).

    Prefer :func:`load_sector_instruments` when you need ``exchange`` (e.g. BSE rows).
    """
    return [i.symbol for i in load_sector_instruments(sector_id, max_symbols=max_symbols)]
