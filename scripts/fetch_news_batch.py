#!/usr/bin/env python3
"""Batch-fetch news for sector universes and optionally refresh symbol snapshots.

Usage:
  python scripts/fetch_news_batch.py --sectors all --refresh-snapshots
  python scripts/fetch_news_batch.py --sectors defence,banking --workers 4
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from news_config import prepare_news_script_config
from news_store import get_symbol_news_snapshot, store_news_items
from sector_registry import list_active_sector_ids, load_sector_instruments

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def fetch_and_store_for_symbol(
    cfg,
    symbol: str,
    exchange: str,
    *,
    refresh_snapshots: bool,
) -> dict:
    try:
        items = fetch_all_news_for_symbol(symbol, exchange, cfg=cfg)
        store_result = store_news_items(cfg, items)
        inserted = int(store_result.get("inserted") or 0)
        if refresh_snapshots and inserted > 0:
            get_symbol_news_snapshot(cfg, symbol, force_refresh=True, exchange=exchange)
        return {
            "symbol": symbol,
            "fetched": len(items),
            "stored": inserted,
            "duplicates": int(store_result.get("duplicates_skipped") or 0),
            "error": None,
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "fetched": 0,
            "stored": 0,
            "duplicates": 0,
            "error": str(exc),
        }


def _resolve_sector_ids(raw: str) -> list[str]:
    text = str(raw or "all").strip().lower()
    if text in ("", "all"):
        return list_active_sector_ids(include_unknown=False)
    return [part.strip().lower() for part in text.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and store news for sector symbols.")
    parser.add_argument("--sectors", default="all", help="Comma-separated sector ids or 'all'.")
    parser.add_argument(
        "--refresh-snapshots",
        action="store_true",
        help="Refresh symbol_news_snapshots when new items are stored.",
    )
    parser.add_argument("--workers", type=int, default=4, help="Parallel worker count (default 4).")
    args = parser.parse_args()

    cfg = prepare_news_script_config()
    sector_ids = _resolve_sector_ids(args.sectors)
    if not sector_ids:
        logger.error("No sectors resolved from --sectors=%r", args.sectors)
        return 1

    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sector_id in sector_ids:
        for inst in load_sector_instruments(sector_id):
            key = (inst.symbol.strip().upper(), inst.exchange.strip().upper())
            if not key[0] or key in seen:
                continue
            seen.add(key)
            pairs.append((inst.symbol, inst.exchange, sector_id))

    if not pairs:
        logger.error("No symbols found for sectors: %s", ", ".join(sector_ids))
        return 1

    workers = max(1, min(int(args.workers), 16))
    totals = {"fetched": 0, "stored": 0, "duplicates": 0, "errors": 0}
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                fetch_and_store_for_symbol,
                cfg,
                symbol,
                exchange,
                refresh_snapshots=args.refresh_snapshots,
            ): symbol
            for symbol, exchange, _sector_id in pairs
        }
        for fut in as_completed(future_map):
            symbol = future_map[fut]
            try:
                result = fut.result(timeout=30)
            except Exception as exc:
                totals["errors"] += 1
                failed.append(symbol)
                logger.warning("News batch failed for %s: %s", symbol, exc)
                continue
            if result.get("error"):
                totals["errors"] += 1
                failed.append(symbol)
                logger.warning("News batch failed for %s: %s", symbol, result["error"])
                continue
            totals["fetched"] += int(result.get("fetched") or 0)
            totals["stored"] += int(result.get("stored") or 0)
            totals["duplicates"] += int(result.get("duplicates") or 0)

    logger.info(
        "News batch complete symbols=%s fetched=%s stored=%s duplicates=%s errors=%s",
        len(pairs),
        totals["fetched"],
        totals["stored"],
        totals["duplicates"],
        totals["errors"],
    )
    if failed:
        logger.warning("Failed symbols (first 10): %s", ", ".join(failed[:10]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
