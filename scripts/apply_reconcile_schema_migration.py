"""Apply reconcile/analytics schema migration via Supabase Management API."""

from __future__ import annotations

import os
import re
import sys
import urllib.error
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sql" / "alter_symbol_daily_features_reconcile_persist.sql"


def _run_management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-Reconcile-Migration/1.0",
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

    if not MIGRATION.is_file():
        print(f"Migration not found: {MIGRATION}", file=sys.stderr)
        return 1

    migration_sql = MIGRATION.read_text(encoding="utf-8")
    try:
        _run_management_query(project_ref, access_token, migration_sql)
        print(f"Applied migration: {MIGRATION.name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Migration failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
