from __future__ import annotations

import json
import logging

from dotenv import load_dotenv

from src.providers.icici_scrip_provider import fetch_instruments_from_scrip_master
from src.supabase_sync import make_client, sync_universe

logger = logging.getLogger("provider-universe-sync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def main() -> int:
    load_dotenv(override=False)
    instruments = fetch_instruments_from_scrip_master()
    if not instruments:
        raise RuntimeError("No instruments loaded from provider source.")
    logger.info("Loaded %s raw instruments from provider source", len(instruments))

    client = make_client()
    counters = sync_universe(client, instruments)
    logger.info("Sync complete: %s", json.dumps(counters, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
