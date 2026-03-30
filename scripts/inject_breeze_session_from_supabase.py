"""
Load BREEZE_SESSION_TOKEN into GITHUB_ENV for GitHub Actions (used by main.py --live).

Resolution order:
1) If env BREEZE_SESSION_TOKEN is non-empty (e.g. repository secret), use it — no Supabase read.
2) Else read from Supabase table `session_config` (requires SUPABASE_URL + service_role SUPABASE_KEY).

Local: typically use .env; this script targets CI.
"""

from __future__ import annotations

import os
import secrets
import sys

from supabase import create_client


def _append_github_env_multiline(name: str, value: str, path: str) -> None:
    """Write name=value safely (handles special characters per Actions file syntax)."""
    if "\n" in value or "\r" in value:
        print("ERROR: token must not contain newlines", file=sys.stderr)
        sys.exit(1)
    delim = secrets.token_hex(16)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def _write_token_to_github_env(token: str, source: str) -> int:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        print("GITHUB_ENV not set; not printing token. Use in Actions only.", file=sys.stderr)
        return 1
    _append_github_env_multiline("BREEZE_SESSION_TOKEN", token, gh_env)
    print(f"Injected BREEZE_SESSION_TOKEN into GITHUB_ENV ({source}).")
    return 0


def main() -> int:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        print("GITHUB_ENV not set; this script is intended for GitHub Actions.", file=sys.stderr)
        return 1

    override = (os.environ.get("BREEZE_SESSION_TOKEN") or "").strip()
    if override:
        return _write_token_to_github_env(override, "repository secret / env")

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        print(
            "Set repository secret BREEZE_SESSION_TOKEN, or supply SUPABASE_URL + SUPABASE_KEY "
            "to read session_config.",
            file=sys.stderr,
        )
        return 1

    client = create_client(url, key)
    res = client.table("session_config").select("breeze_session_token").limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        print(
            "session_config returned no rows. Fix one of:\n"
            "  1) Repository secret BREEZE_SESSION_TOKEN (paste daily Breeze token).\n"
            "  2) Supabase SQL: run sql/create_session_config.sql then set breeze_session_token.\n"
            "  3) GitHub secret SUPABASE_KEY = service_role (not anon) if using RLS.",
            file=sys.stderr,
        )
        return 1
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        print(
            "breeze_session_token is empty in Supabase; set repository secret BREEZE_SESSION_TOKEN or edit Table Editor.",
            file=sys.stderr,
        )
        return 1

    return _write_token_to_github_env(token, "Supabase session_config")


if __name__ == "__main__":
    raise SystemExit(main())
