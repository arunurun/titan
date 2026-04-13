"""One-time backfill: seed Supabase sector tables from data/sectors/*.csv."""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
SECTORS_DIR = ROOT / "data" / "sectors"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _load_rows(csv_path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {csv_path}")
        if "symbol" not in {x.strip().lower() for x in reader.fieldnames if x}:
            raise ValueError(f"CSV missing symbol column: {csv_path}")
        for row in reader:
            symbol = str(row.get("symbol", "")).strip().upper()
            exchange = str(row.get("exchange", "NSE")).strip().upper() or "NSE"
            if not symbol or symbol.startswith("#"):
                continue
            if exchange not in {"NSE", "BSE"}:
                raise ValueError(f"Invalid exchange {exchange!r} in {csv_path}")
            rows.append((symbol, exchange))
    return rows


def _upsert_sector(client, sector_key: str) -> str:
    client.table("sector_catalog").upsert(
        {
            "sector_key": sector_key,
            "sector_name": sector_key.replace("_", " ").title(),
            "is_active": True,
        },
        on_conflict="sector_key",
    ).execute()
    result = (
        client.table("sector_catalog")
        .select("id")
        .eq("sector_key", sector_key)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise RuntimeError(f"Could not resolve sector id for {sector_key}")
    return rows[0]["id"]


def _upsert_instrument(client, symbol: str, exchange: str) -> str:
    client.table("market_instruments").upsert(
        {"symbol": symbol, "exchange": exchange, "is_active": True},
        on_conflict="exchange,symbol",
    ).execute()
    result = (
        client.table("market_instruments")
        .select("id")
        .eq("exchange", exchange)
        .eq("symbol", symbol)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise RuntimeError(f"Could not resolve instrument id for {symbol} ({exchange})")
    return rows[0]["id"]


def main() -> int:
    supabase_url = _require_env("SUPABASE_URL")
    supabase_key = _require_env("SUPABASE_KEY")
    client = create_client(supabase_url, supabase_key)

    if not SECTORS_DIR.is_dir():
        raise FileNotFoundError(f"Sector directory not found: {SECTORS_DIR}")

    total_links = 0
    for csv_path in sorted(SECTORS_DIR.glob("*.csv")):
        sector_key = csv_path.stem.strip().lower()
        if not sector_key:
            continue
        symbols = _load_rows(csv_path)
        sector_id = _upsert_sector(client, sector_key)

        for symbol, exchange in symbols:
            instrument_id = _upsert_instrument(client, symbol, exchange)
            client.table("instrument_sector_map").upsert(
                {
                    "instrument_id": instrument_id,
                    "sector_id": sector_id,
                    "source": "override",
                    "is_active": True,
                },
                on_conflict="instrument_id,sector_id",
            ).execute()
            total_links += 1

        print(f"Backfilled sector={sector_key} rows={len(symbols)}")

    print(f"Backfill complete. total_links={total_links}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Backfill failed: {exc}", file=sys.stderr)
        raise
