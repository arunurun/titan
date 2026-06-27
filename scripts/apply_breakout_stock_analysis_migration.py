"""Apply breakout_stock_analysis DDL via the Supabase Management API.

Mirrors scripts/apply_live_tables_migration.py. Requires SUPABASE_ACCESS_TOKEN
because PostgREST cannot run DDL.
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

MIGRATIONS = [
    ROOT / "sql" / "create_breakout_stock_analysis.sql",
    ROOT / "sql" / "add_breakout_v7_columns.sql",
]

VERIFY_TABLES_SQL = """
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name = 'breakout_stock_analysis';
"""

VERIFY_COUNT_SQL = """
select count(*)::int as row_count
from public.breakout_stock_analysis;
"""


def _run_management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-BreakoutStockAnalysis-Migration/1.0",
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
            "Set a Supabase management token (or run sql/create_breakout_stock_analysis.sql in "
            "the Supabase SQL editor) then re-run this script.",
            file=sys.stderr,
        )
        return 1

    match = re.search(r"https://([^.]+)\.supabase\.co", cfg.supabase_url)
    if not match:
        print("Could not parse project ref from SUPABASE_URL", file=sys.stderr)
        return 1
    project_ref = match.group(1)

    for migration in MIGRATIONS:
        if not migration.is_file():
            print(f"Migration not found: {migration}", file=sys.stderr)
            return 1

    try:
        for migration in MIGRATIONS:
            _run_management_query(project_ref, access_token, migration.read_text(encoding="utf-8"))
            print(f"Applied migration: {migration.name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Migration failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1

    verify = _run_management_query(project_ref, access_token, VERIFY_TABLES_SQL)
    rows = verify if isinstance(verify, list) else []
    print(f"Verified table: {json.dumps(rows, default=str)}")
    if len(rows) < 1:
        print("Expected public.breakout_stock_analysis; table not found after migration.", file=sys.stderr)
        return 1

    count = _run_management_query(project_ref, access_token, VERIFY_COUNT_SQL)
    print(f"Row count: {json.dumps(count, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
