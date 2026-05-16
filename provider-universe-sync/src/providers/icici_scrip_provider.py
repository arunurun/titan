from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import requests

from src.models import UniverseInstrument, normalize_sector_key

DEFAULT_SCRIP_MASTER_URL = (
    "https://traderweb.icicidirect.com/Content/File/txtFile/ScripFile/StockScriptNew.csv"
)
NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
NSE_HOME_URL = "https://www.nseindia.com"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "nse_industry_cache.json"


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
                official_industry=str(
                    row.get("INDUSTRY")
                    or row.get("IND")
                    or row.get("industry")
                    or ""
                ).strip(),
            )
        )
    if (os.environ.get("NSE_INDUSTRY_ENRICH", "1").strip().lower() not in {"0", "false", "no"}):
        out = _enrich_nse_industry(out)
    return out


def _load_industry_cache() -> dict[str, str]:
    if not CACHE_PATH.is_file():
        return {}
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in payload.items() if str(k).strip() and str(v).strip()}


def _save_industry_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=True, sort_keys=True), encoding="utf-8")


def _fetch_industry_for_symbol(session: requests.Session, symbol: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
    }
    resp = session.get(NSE_QUOTE_URL.format(symbol=symbol), headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    info = payload.get("info") if isinstance(payload, dict) else {}
    return str((info or {}).get("industry") or "").strip()


def _enrich_nse_industry(instruments: list[UniverseInstrument]) -> list[UniverseInstrument]:
    cache = _load_industry_cache()
    symbols = sorted({i.symbol for i in instruments if i.exchange == "NSE"})
    limit_raw = (os.environ.get("NSE_INDUSTRY_ENRICH_LIMIT") or "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else None
    target = symbols if limit is None else symbols[: max(0, limit)]
    to_fetch = [s for s in target if not cache.get(s)]
    if to_fetch:
        session = requests.Session()
        session.get(NSE_HOME_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        for sym in to_fetch:
            try:
                industry = _fetch_industry_for_symbol(session, sym)
                if industry:
                    cache[sym] = industry
            except Exception:  # noqa: BLE001
                continue
        _save_industry_cache(cache)
    enriched: list[UniverseInstrument] = []
    for inst in instruments:
        if inst.exchange != "NSE":
            enriched.append(inst)
            continue
        industry = inst.official_industry.strip() or cache.get(inst.symbol, "")
        enriched.append(
            UniverseInstrument(
                exchange=inst.exchange,
                symbol=inst.symbol,
                instrument_name=inst.instrument_name,
                isin=inst.isin,
                official_sector_key=inst.official_sector_key,
                official_industry=industry,
            )
        )
    return enriched
