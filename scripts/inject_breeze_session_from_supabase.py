"""
Load BREEZE_SESSION_TOKEN from Supabase `session_config` into GITHUB_ENV (GitHub Actions).

Requires: SUPABASE_URL, SUPABASE_KEY (service role recommended), table session_config row id=1.

Local test: set env and run without GITHUB_ENV — prints token to stdout only if safe (avoid in CI logs).
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


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_KEY are required", file=sys.stderr)
        return 1

    client = create_client(url, key)
    res = client.table("session_config").select("breeze_session_token").eq("id", 1).limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        print(
            "session_config has no row id=1; run sql/create_session_config.sql and set breeze_session_token.",
            file=sys.stderr,
        )
        return 1
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        print("breeze_session_token is empty; paste today token in Supabase Table Editor.", file=sys.stderr)
        return 1

    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        _append_github_env_multiline("BREEZE_SESSION_TOKEN", token, gh_env)
        print("Injected BREEZE_SESSION_TOKEN into GITHUB_ENV for subsequent steps.")
    else:
        print("GITHUB_ENV not set; not printing token. Use in Actions only.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
