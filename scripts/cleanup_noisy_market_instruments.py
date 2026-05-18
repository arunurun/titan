#!/usr/bin/env python3
"""
Deactivate noisy rows in market_instruments / instrument_sector_map using the same
rules as provider-universe-sync sync (see src/equity_filter.py).

Requires: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY with RLS bypass).

  python scripts/cleanup_noisy_market_instruments.py --dry-run
  python scripts/cleanup_noisy_market_instruments.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

_ROOT = Path(__file__).resolve().parents[1]
_PROV = _ROOT / "provider-universe-sync"
sys.path.insert(0, str(_PROV))

from src.equity_filter import is_meaningful_listed_equity  # noqa: E402
from src.models import UniverseInstrument  # noqa: E402

CHUNK = 400


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY).")
    return create_client(url, key)


def _fetch_active_rows(client):
    out: list[dict] = []
    offset = 0
    page = 1000
    while True:
        q = (
            client.table("market_instruments")
            .select("id,exchange,symbol,instrument_name,isin")
            .eq("is_active", True)
            .order("id")
            .range(offset, offset + page - 1)
        )
        rows = q.execute().data or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def _chunked(xs: list[str], n: int) -> list[list[str]]:
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def main() -> int:
    load_dotenv(override=False)
    titan_env = _ROOT / ".env"
    if titan_env.is_file():
        load_dotenv(titan_env, override=False)

    ap = argparse.ArgumentParser(description="Deactivate noisy market_instruments rows.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Print counts and samples only.")
    g.add_argument("--apply", action="store_true", help="Set is_active=false on noise rows.")
    args = ap.parse_args()

    client = _client()
    rows = _fetch_active_rows(client)
    noise_ids: list[str] = []
    samples: list[tuple[str, str, str, str]] = []

    for row in rows:
        inst = UniverseInstrument(
            exchange=str(row.get("exchange") or ""),
            symbol=str(row.get("symbol") or ""),
            instrument_name=str(row.get("instrument_name") or ""),
            isin=str(row.get("isin") or ""),
            official_sector_key="unknown",
            official_industry="",
        )
        if is_meaningful_listed_equity(inst):
            continue
        iid = str(row.get("id") or "").strip()
        if not iid:
            continue
        noise_ids.append(iid)
        if len(samples) < 25:
            samples.append(
                (
                    inst.exchange,
                    inst.symbol,
                    (inst.instrument_name or "")[:60],
                    inst.isin or "",
                )
            )

    print(f"Active instruments scanned: {len(rows)}")
    print(f"Noise rows (would deactivate): {len(noise_ids)}")
    for ex, sym, name, isin in samples:
        print(f"  sample: {ex} {sym} | {name!r} | {isin!r}")

    if args.dry_run:
        return 0

    for chunk in _chunked(noise_ids, CHUNK):
        client.table("market_instruments").update({"is_active": False}).in_("id", chunk).execute()
        client.table("instrument_sector_map").update({"is_active": False}).in_("instrument_id", chunk).execute()

    print(f"Deactivated {len(noise_ids)} instruments and their sector map rows (by instrument_id).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
