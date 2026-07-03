"""Apply the equity_ohlcv_daily table migration via the Supabase Management API.

Mirrors scripts/apply_shareholding_quarterly_migration.py. Requires SUPABASE_ACCESS_TOKEN
(a Supabase personal/management token) because PostgREST cannot run DDL.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config_loader import load_config  # noqa: E402

MIGRATION = ROOT / "sql" / "create_equity_ohlcv_daily.sql"

VERIFY_TABLES_SQL = """
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name = 'equity_ohlcv_daily';
"""


def _run_management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-Equity-OHLCV-Migration/1.0",
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
            resp.url, resp.status_code, resp.reason or "HTTP error", resp.headers, resp.content
        )
    if not resp.text.strip():
        return []
    return resp.json()


def main() -> int:
    try:
        cfg = load_config(require_breeze=False, require_gemini=False)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not access_token:
        print(
            "Missing SUPABASE_ACCESS_TOKEN — DDL cannot run via PostgREST. "
            "Set a Supabase management token (or run sql/create_equity_ohlcv_daily.sql in the "
            "Supabase SQL editor) then re-run scripts/ingest_breakout_ohlcv.py.",
            file=sys.stderr,
        )
        return 1

    match = re.search(r"https://([^.]+)\.supabase\.co", cfg.supabase_url)
    if not match:
        print("Could not parse project ref from SUPABASE_URL", file=sys.stderr)
        return 1
    project_ref = match.group(1)

    if not MIGRATION.is_file():
        print(f"Migration not found: {MIGRATION}", file=sys.stderr)
        return 1

    try:
        _run_management_query(project_ref, access_token, MIGRATION.read_text(encoding="utf-8"))
        print(f"Applied migration: {MIGRATION.name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Migration failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1

    verify = _run_management_query(project_ref, access_token, VERIFY_TABLES_SQL)
    rows = verify if isinstance(verify, list) else []
    print(f"Verified equity_ohlcv_daily: {json.dumps(rows, default=str)}")
    if len(rows) < 1:
        print("Expected equity_ohlcv_daily table; re-run or check schema.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
