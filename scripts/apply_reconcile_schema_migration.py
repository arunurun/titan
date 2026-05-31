"""Apply reconcile/analytics schema migration via Supabase Management API."""

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

MIGRATION = ROOT / "sql" / "alter_symbol_daily_features_reconcile_persist.sql"

VERIFY_COLUMNS_SQL = """
select table_name, column_name, data_type, is_nullable
from information_schema.columns
where table_schema = 'public'
  and table_name in ('symbol_daily_features', 'sector_daily_rollup')
  and column_name in (
    'tape_extras','action_signal','volume_participation_ratio',
    'next_day_score','next_week_score','news_correlation',
    'news_sentiment_aggregate','news_sentiment_score',
    'news_sentiment_trend','news_count','pct_volume_participation_gt_1'
  )
order by table_name, column_name;
"""


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
    try:
        cfg = load_config(require_breeze=False, require_gemini=False)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not access_token:
        print("Missing SUPABASE_ACCESS_TOKEN", file=sys.stderr)
        return 1

    match = re.search(r"https://([^.]+)\.supabase\.co", cfg.supabase_url)
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

    verify = _run_management_query(project_ref, access_token, VERIFY_COLUMNS_SQL)
    rows = verify if isinstance(verify, list) else []
    print(f"Verified {len(rows)} reconcile column(s)")
    print(json.dumps(rows, default=str))
    if len(rows) < 11:
        print(
            f"Expected 11 reconcile columns; got {len(rows)}. Re-run migration or check schema.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
