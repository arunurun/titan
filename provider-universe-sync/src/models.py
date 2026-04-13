from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseInstrument:
    exchange: str
    symbol: str
    instrument_name: str
    isin: str
    official_sector_key: str


def normalize_sector_key(value: str) -> str:
    cleaned = (value or "").strip().lower().replace("&", " and ")
    cleaned = "_".join(cleaned.split())
    return cleaned or "unknown"
