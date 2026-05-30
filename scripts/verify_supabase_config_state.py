"""Print session_config / titan_secrets state (lengths only; no secret values)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "config" / ".env", override=False)
    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    if not access_token or not supabase_url:
        print("Missing SUPABASE_ACCESS_TOKEN or SUPABASE_URL", file=sys.stderr)
        return 1
    match = re.search(r"https://([^.]+)\.supabase\.co", supabase_url)
    if not match:
        print("Could not parse project ref from SUPABASE_URL", file=sys.stderr)
        return 1
    project_ref = match.group(1)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-Verify/1.0",
    }
    for label, query in (
        (
            "session_config",
            """
select id, length(breeze_session_token) as breeze_len, updated_at
from public.session_config order by id;
""",
        ),
        (
            "session_config_columns",
            """
select column_name from information_schema.columns
where table_schema = 'public' and table_name = 'session_config'
order by ordinal_position;
""",
        ),
        (
            "titan_secrets",
            """
select key_name, length(value) as value_len, updated_at
from public.titan_secrets order by key_name;
""",
        ),
    ):
        resp = requests.post(
            f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
            headers=headers,
            json={"query": query},
            timeout=120,
        )
        if resp.status_code >= 400:
            print(f"{label}: HTTP {resp.status_code}", file=sys.stderr)
            return 1
        print(f"{label}:", json.dumps(resp.json(), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
