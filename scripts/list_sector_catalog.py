"""Print active sector_key list from Supabase sector_catalog (JSON)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        print("Missing SUPABASE_URL/SUPABASE_KEY", file=sys.stderr)
        return 1
    client = create_client(url, key)
    res = (
        client.table("sector_catalog")
        .select("sector_key,sector_name,is_active")
        .eq("is_active", True)
        .order("sector_key")
        .execute()
    )
    rows = list(getattr(res, "data", None) or [])
    keys = [str(r.get("sector_key", "")).strip().lower() for r in rows if isinstance(r, dict)]
    print(json.dumps({"count": len(keys), "sector_keys": keys, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
