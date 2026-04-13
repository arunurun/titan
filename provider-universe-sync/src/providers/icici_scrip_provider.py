from __future__ import annotations

import csv
import io
import os

import requests

from src.models import UniverseInstrument, normalize_sector_key

DEFAULT_SCRIP_MASTER_URL = (
    "https://traderweb.icicidirect.com/Content/File/txtFile/ScripFile/StockScriptNew.csv"
)


def fetch_instruments_from_scrip_master() -> list[UniverseInstrument]:
    url = os.environ.get("SCRIP_MASTER_URL", "").strip() or DEFAULT_SCRIP_MASTER_URL
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))

    out: list[UniverseInstrument] = []
    for row in reader:
        exchange = str(row.get("EC", "")).strip().upper()
        symbol = str(row.get("NS", "")).strip().upper()
        if exchange not in {"NSE", "BSE"} or not symbol:
            continue
        out.append(
            UniverseInstrument(
                exchange=exchange,
                symbol=symbol,
                instrument_name=str(row.get("SN", "")).strip(),
                isin=str(row.get("ISIN", "")).strip(),
                # Scrip source does not provide robust sector coverage.
                # If any sector-like tag is present upstream, we keep it.
                official_sector_key=normalize_sector_key(str(row.get("SECTOR", "")).strip()),
            )
        )
    return out
