"""Print BREEZE_SESSION_TOKEN from Supabase session_config (for local shell injection)."""

from __future__ import annotations

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
        print("Missing SUPABASE_URL or SUPABASE_KEY", file=sys.stderr)
        return 1
    client = create_client(url, key)
    res = client.table("session_config").select("breeze_session_token").eq("id", 1).limit(1).execute()
    data = list(getattr(res, "data", None) or [])
    if not data:
        print("No session_config row id=1", file=sys.stderr)
        return 1
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        print("breeze_session_token empty in session_config", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
