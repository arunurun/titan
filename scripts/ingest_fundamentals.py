#!/usr/bin/env python3
"""Ingest fundamental metrics into market_instruments from Yahoo Finance (yfinance).

Fetches profitability, leverage, growth, and valuation fields for active NSE/BSE equities
and updates fundamental columns used by ``fundamental_engine.score_fundamentals``.

Usage:
  python scripts/ingest_fundamentals.py --all
  python scripts/ingest_fundamentals.py --sector pharma_healthcare
  python scripts/ingest_fundamentals.py --symbol LAURUSLABS
  python scripts/ingest_fundamentals.py --all --dry-run

Set TITAN_FUNDAMENTALS_INGEST_THROTTLE_SEC for a politeness delay between yfinance calls.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from sector_registry import SectorInstrument, load_sector_instruments
from supabase import create_client

FUNDAMENTALS_SOURCE = "yfinance"
BATCH_SIZE = 25


def _throttle_sec() -> float:
    raw = os.environ.get("TITAN_FUNDAMENTALS_INGEST_THROTTLE_SEC", "0.35").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.35
    except ValueError:
        return 0.35


def _sf(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _as_pct(v: Any) -> float | None:
    """Normalize yfinance ratios (0.15) or already-percent values (15.0)."""
    x = _sf(v)
    if x is None:
        return None
    if abs(x) <= 1.5:
        return round(x * 100.0, 4)
    return round(x, 4)


def _yahoo_ticker(inst: SectorInstrument) -> str:
    if inst.exchange == "NSE":
        return f"{inst.symbol}.NS"
    if inst.exchange == "BSE":
        return f"{inst.symbol}.BO"
    raise ValueError(f"unsupported exchange {inst.exchange!r}")


def _fcf_yield_pct(fcf: float | None, mcap: float | None) -> float | None:
    if fcf is None or mcap is None or mcap <= 0:
        return None
    return round((fcf / mcap) * 100.0, 4)


def _debt_to_equity(v: Any) -> float | None:
    x = _sf(v)
    if x is None:
        return None
    # yfinance Indian tickers sometimes report D/E as percent (e.g. 46.4 vs 0.46).
    if x > 5.0:
        x = x / 100.0
    return round(x, 4)


def _map_yfinance_info(info: dict[str, Any], *, as_of: date) -> dict[str, Any]:
    roe = _as_pct(info.get("returnOnEquity"))
    roce = None  # yfinance does not expose ROCE for Indian tickers
    debt_to_equity = _debt_to_equity(info.get("debtToEquity"))
    net_profit_margin = _as_pct(info.get("profitMargins"))
    operating_margin = _as_pct(info.get("operatingMargins"))
    revenue_growth_pct = _as_pct(info.get("revenueGrowth"))
    eps_growth_pct = _as_pct(info.get("earningsGrowth"))
    pe_ratio = _sf(info.get("trailingPE") or info.get("forwardPE"))
    peg_ratio = _sf(info.get("pegRatio"))
    price_to_sales = _sf(info.get("priceToSalesTrailing12Months"))
    free_cash_flow = _sf(info.get("freeCashflow"))
    market_cap = _sf(info.get("marketCap"))
    fcf_yield = _fcf_yield_pct(free_cash_flow, market_cap)

    fields = {
        "roe": roe,
        "roce": roce,
        "debt_to_equity": debt_to_equity,
        "net_profit_margin": net_profit_margin,
        "operating_margin": operating_margin,
        "revenue_growth_pct": revenue_growth_pct,
        "eps_growth_pct": eps_growth_pct,
        "pe_ratio": pe_ratio,
        "peg_ratio": peg_ratio,
        "price_to_sales": price_to_sales,
        "free_cash_flow": free_cash_flow,
        "market_cap": market_cap,
        "fcf_yield_pct": fcf_yield,
        "fundamentals_as_of": as_of.isoformat(),
        "fundamentals_source": FUNDAMENTALS_SOURCE,
    }
    return {k: v for k, v in fields.items() if v is not None}


def _has_scoring_field(payload: dict[str, Any]) -> bool:
    scoring_keys = (
        "roe",
        "roce",
        "debt_to_equity",
        "net_profit_margin",
        "operating_margin",
        "revenue_growth_pct",
        "eps_growth_pct",
        "pe_ratio",
        "peg_ratio",
        "free_cash_flow",
        "market_cap",
        "fcf_yield_pct",
    )
    return any(k in payload for k in scoring_keys)


def fetch_fundamentals(inst: SectorInstrument) -> dict[str, Any] | None:
    import yfinance as yf

    ticker = _yahoo_ticker(inst)
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict) or not info:
        return None
    payload = _map_yfinance_info(info, as_of=date.today())
    if not _has_scoring_field(payload):
        return None
    return payload


def _dedupe_primary_exchange(instruments: list[SectorInstrument]) -> list[SectorInstrument]:
    seen: set[str] = set()
    out: list[SectorInstrument] = []
    for inst in instruments:
        if inst.exchange != "NSE":
            continue
        if inst.symbol in seen:
            continue
        seen.add(inst.symbol)
        out.append(inst)
    for inst in instruments:
        if inst.exchange != "BSE":
            continue
        if inst.symbol in seen:
            continue
        seen.add(inst.symbol)
        out.append(inst)
    return out


def load_instruments(
    client,
    *,
    sector: str | None,
    all_active: bool,
    symbol: str | None,
) -> list[SectorInstrument]:
    if sector:
        rows = load_sector_instruments(sector)
    elif all_active:
        res = (
            client.table("market_instruments")
            .select("symbol,exchange")
            .eq("is_active", True)
            .in_("exchange", ["NSE", "BSE"])
            .execute()
        )
        raw = list(getattr(res, "data", None) or [])
        rows = [
            SectorInstrument(
                symbol=str(r.get("symbol", "")).strip().upper(),
                exchange=str(r.get("exchange", "")).strip().upper(),
            )
            for r in raw
            if str(r.get("symbol", "")).strip() and str(r.get("exchange", "")).strip().upper() in {"NSE", "BSE"}
        ]
    else:
        raise ValueError("Specify --sector, --all, or --symbol")

    rows = _dedupe_primary_exchange(rows)
    if symbol:
        sym = symbol.strip().upper()
        rows = [r for r in rows if r.symbol == sym]
    return rows


def update_fundamentals(client, inst: SectorInstrument, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        client.table("market_instruments").update(payload).eq("exchange", inst.exchange).eq(
            "symbol", inst.symbol
        ).execute()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"error:{type(exc).__name__}:{exc}"


def ingest_fundamentals(
    client,
    instruments: list[SectorInstrument],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    throttle = _throttle_sec()
    stats = {"seen": 0, "updated": 0, "skipped": 0, "errors": 0}

    for i, inst in enumerate(instruments, start=1):
        stats["seen"] += 1
        payload = fetch_fundamentals(inst)
        if payload is None:
            stats["skipped"] += 1
            print(f"  [{i}/{len(instruments)}] {inst.symbol} ({inst.exchange}): skipped (no data)")
        elif dry_run:
            stats["updated"] += 1
            keys = ", ".join(sorted(k for k in payload if k not in ("fundamentals_as_of", "fundamentals_source")))
            print(f"  [{i}/{len(instruments)}] {inst.symbol} ({inst.exchange}): dry-run ({keys})")
        else:
            ok, status = update_fundamentals(client, inst, payload)
            if ok:
                stats["updated"] += 1
                print(f"  [{i}/{len(instruments)}] {inst.symbol} ({inst.exchange}): updated ({status})")
            else:
                stats["errors"] += 1
                print(f"  [{i}/{len(instruments)}] {inst.symbol} ({inst.exchange}): {status}")

        if throttle and i < len(instruments):
            time.sleep(throttle)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest fundamentals into market_instruments (yfinance)")
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="All active market_instruments")
    scope.add_argument("--sector", type=str, default="", help="Sector key e.g. pharma_healthcare")
    ap.add_argument("--symbol", type=str, default="", help="Optional single symbol filter")
    ap.add_argument("--dry-run", action="store_true", help="Fetch only; do not write to Supabase")
    args = ap.parse_args()

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    sector = args.sector.strip().lower() or None
    symbol = args.symbol.strip().upper() or None
    label = sector or ("all" if args.all else "custom")
    print(f"Ingesting fundamentals scope={label} symbol={symbol or 'all'} dry_run={args.dry_run}")

    try:
        instruments = load_instruments(
            client,
            sector=sector,
            all_active=bool(args.all),
            symbol=symbol,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load instruments: {exc}", file=sys.stderr)
        return 1

    if not instruments:
        print("No instruments matched.", file=sys.stderr)
        return 1

    print(f"Loaded {len(instruments)} instruments")
    stats = ingest_fundamentals(client, instruments, dry_run=args.dry_run)
    print(
        f"DONE. seen={stats['seen']} updated={stats['updated']} "
        f"skipped={stats['skipped']} errors={stats['errors']}"
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
