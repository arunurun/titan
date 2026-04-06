"""Load sector stock lists from CSV under data/sectors/ (NSE and/or BSE).

Sector CSVs may mix large-, mid-, and small-cap names and both exchanges for broader screening;
liquidity and data quality still vary—callers should handle failures per instrument.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTORS_DIR = ROOT / "data" / "sectors"

# Cap how many instruments we process per run (raise locally when scaling sector work).
# defence.csv may grow; keep this >= current row count so names are not silently dropped.
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
    Read ``data/sectors/<sector_id>.csv`` (lowercase filename).

    Required column: ``symbol``. Optional column: ``exchange`` (``NSE`` or ``BSE``); defaults to ``NSE``.
    Rows with empty symbols are skipped.
    When ``max_symbols`` is None, uses :data:`MAX_SYMBOLS`.
    """
    cap = max_symbols if max_symbols is not None else MAX_SYMBOLS
    if cap < 0:
        raise ValueError("max_symbols must be >= 0")

    sid = sector_id.strip().lower()
    if not sid:
        raise ValueError("sector_id must be non-empty")

    path = SECTORS_DIR / f"{sid}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"No sector file for '{sector_id}': {path}")

    rows: list[SectorInstrument] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header row: {path}")
        fields = {((n or "").strip().lower()): n for n in reader.fieldnames}
        if "symbol" not in fields:
            raise ValueError(f"CSV must have a 'symbol' column: {path}")
        sym_col = fields["symbol"]
        exch_col = fields.get("exchange")

        for row in reader:
            raw_sym = (row.get(sym_col) or "").strip()
            if not raw_sym or raw_sym.startswith("#"):
                continue
            sym = raw_sym.upper()

            if exch_col is not None:
                raw_ex = (row.get(exch_col) or "").strip().upper()
                exch = raw_ex if raw_ex else "NSE"
            else:
                exch = "NSE"

            if exch not in _EXCHANGES:
                raise ValueError(
                    f"Invalid exchange {exch!r} for {sym} in {path} (use NSE or BSE)"
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
