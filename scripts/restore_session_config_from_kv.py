"""Revert session_config KV merge; verify id=1 only (never logs secret values)."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _run_management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-Restore/1.0",
        "Accept": "application/json",
    }
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
        headers=headers,
        json={"query": query},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise urllib.error.HTTPError(
            resp.url,
            resp.status_code,
            resp.reason or "HTTP error",
            resp.headers,
            resp.content,
        )
    if not resp.text.strip():
        return []
    return resp.json()


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

    restore_path = ROOT / "sql" / "restore_session_config_from_kv.sql"
    restore_sql = restore_path.read_text(encoding="utf-8")
    try:
        _run_management_query(project_ref, access_token, restore_sql)
        print(f"Applied restore: {restore_path.name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Restore failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1

    verify = _run_management_query(
        project_ref,
        access_token,
        """
select id, length(breeze_session_token) as breeze_len, updated_at
from public.session_config
order by id;

select column_name
from information_schema.columns
where table_schema = 'public' and table_name = 'session_config'
order by ordinal_position;
""",
    )
    print("Verification:", json.dumps(verify, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
