"""Apply titan_secrets migration and optionally upsert keys from env (never commit keys)."""

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

KEY_DESCRIPTIONS: dict[str, str] = {
    "NEWSAPI_API_KEY": "NewsAPI.org key for per-symbol news fetch",
    "FINNHUB_API_KEY": "Finnhub API key for company news",
    "SUPABASE_URL": "Supabase project URL (bootstrap + runtime)",
    "SUPABASE_KEY": "Supabase service_role key (server-side only)",
    "TITAN_NEWS_FEEDS": "Comma-separated RSS feed URLs for news ingestion",
}


def _run_management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Titan-Migration/1.0",
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


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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

    migration_path = ROOT / "sql" / "create_titan_secrets.sql"
    migration_sql = migration_path.read_text(encoding="utf-8")
    try:
        _run_management_query(project_ref, access_token, migration_sql)
        print(f"Applied migration: {migration_path.name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Migration failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1

    upsert_pairs: list[tuple[str, str]] = []
    for name in (
        "NEWSAPI_API_KEY",
        "FINNHUB_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "TITAN_NEWS_FEEDS",
    ):
        val = os.environ.get(name, "").strip()
        if val:
            upsert_pairs.append((name, val))

    if upsert_pairs:
        values_sql = ",\n  ".join(
            f"({_sql_literal(name)}, {_sql_literal(val)}, now(), {_sql_literal(KEY_DESCRIPTIONS.get(name, ''))})"
            for name, val in upsert_pairs
        )
        upsert_sql = f"""
insert into public.titan_secrets (key_name, value, updated_at, description)
values
  {values_sql}
on conflict (key_name) do update
  set value = excluded.value,
      updated_at = now(),
      description = coalesce(excluded.description, public.titan_secrets.description);
"""
        try:
            _run_management_query(project_ref, access_token, upsert_sql)
            print(f"Upserted {len(upsert_pairs)} key(s) into public.titan_secrets")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            print(f"Upsert failed: HTTP {exc.code} {detail}", file=sys.stderr)
            return 1
    else:
        print("No news/CI keys in env; skipped upsert")

    names_in = ", ".join(_sql_literal(n) for n in KEY_DESCRIPTIONS)
    verify = _run_management_query(
        project_ref,
        access_token,
        f"""
select key_name, length(value) as value_len, updated_at, description
from public.titan_secrets
where key_name in ({names_in})
order by key_name;
""",
    )
    print("Verification:", json.dumps(verify, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
