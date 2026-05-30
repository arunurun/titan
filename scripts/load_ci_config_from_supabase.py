"""
Load news/CI config from Supabase into GITHUB_ENV for GitHub Actions.

Bootstrap (one of):
  - SUPABASE_URL + SUPABASE_KEY (service_role): PostgREST read of public.titan_secrets
  - SUPABASE_URL + SUPABASE_ACCESS_TOKEN: Management API database/query (single GHA secret)

Never logs secret values. Intended to run before news fetch/cleanup steps.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from news_config import NEWS_RUNTIME_KEY_NAMES, TITAN_SECRETS_TABLE

try:
    import requests
    from supabase import create_client
except ImportError:
    print("Missing dependencies: pip install supabase requests", file=sys.stderr)
    raise


def _append_github_env_kv(name: str, value: str, path: str) -> None:
    if "\n" in value or "\r" in value:
        print(f"ERROR: {name} must not contain newlines", file=sys.stderr)
        sys.exit(1)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _management_query(project_ref: str, access_token: str, query: str) -> list | dict:
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "Titan-CI-Config/1.0",
            "Accept": "application/json",
        },
        json={"query": query},
        timeout=120,
    )
    if resp.status_code >= 400:
        print(f"Management API query failed: HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    if not resp.text.strip():
        return []
    return resp.json()


def _load_via_postgrest(url: str, key: str) -> dict[str, str]:
    client = create_client(url, key)
    res = (
        client.table(TITAN_SECRETS_TABLE)
        .select("key_name,value")
        .in_("key_name", list(NEWS_RUNTIME_KEY_NAMES))
        .execute()
    )
    out: dict[str, str] = {}
    for row in getattr(res, "data", None) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("key_name") or "").strip()
        if name:
            out[name] = str(row.get("value") or "").strip()
    return out


def _load_via_management_api(url: str, access_token: str) -> dict[str, str]:
    match = re.search(r"https://([^.]+)\.supabase\.co", url)
    if not match:
        print("Could not parse project ref from SUPABASE_URL", file=sys.stderr)
        sys.exit(1)
    project_ref = match.group(1)
    names_in = ", ".join(_sql_literal(n) for n in NEWS_RUNTIME_KEY_NAMES)
    query = f"""
select key_name, value
from public.{TITAN_SECRETS_TABLE}
where key_name in ({names_in});
"""
    rows = _management_query(project_ref, access_token, query)
    out: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("key_name") or "").strip()
            if name:
                out[name] = str(row.get("value") or "").strip()
    return out


def main() -> int:
    gh_env = os.environ.get("GITHUB_ENV", "").strip()
    if not gh_env:
        print("GITHUB_ENV not set; this script is intended for GitHub Actions.", file=sys.stderr)
        return 1

    url = os.environ.get("SUPABASE_URL", "").strip()
    service_key = os.environ.get("SUPABASE_KEY", "").strip()
    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()

    if not url:
        print("Missing SUPABASE_URL (workflow env or repository variable).", file=sys.stderr)
        return 1

    if service_key:
        loaded = _load_via_postgrest(url, service_key)
        source = "postgrest:service_role"
    elif access_token:
        loaded = _load_via_management_api(url, access_token)
        source = "management_api"
    else:
        print(
            "Bootstrap required: set SUPABASE_KEY (service_role) or SUPABASE_ACCESS_TOKEN "
            "(Management API) in addition to SUPABASE_URL.",
            file=sys.stderr,
        )
        return 1

    written: list[str] = []
    for name in NEWS_RUNTIME_KEY_NAMES:
        if os.environ.get(name, "").strip():
            continue
        value = (loaded.get(name) or "").strip()
        if not value:
            continue
        _append_github_env_kv(name, value, gh_env)
        written.append(name)

    print(
        f"Loaded {len(written)} key(s) into GITHUB_ENV from {source} "
        f"(table={TITAN_SECRETS_TABLE}, keys={written})."
    )
    if not written:
        missing = [n for n in NEWS_RUNTIME_KEY_NAMES if not os.environ.get(n, "").strip()]
        if missing:
            print(
                "Warning: no new keys written; still missing in env:",
                json.dumps(missing),
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
