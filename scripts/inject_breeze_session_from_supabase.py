"""
Load BREEZE_SESSION_TOKEN into GITHUB_ENV for GitHub Actions (used by main.py --live).

Used when the workflow does not already inject from repository secret BREEZE_SESSION_TOKEN
(see market_audit.yml: bash writes GITHUB_ENV first; this script handles Supabase-only path).

Requires: SUPABASE_URL + SUPABASE_KEY (must be service_role JWT if RLS is on), table session_config.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys

from supabase import create_client


def jwt_role_from_supabase_key(key: str) -> str | None:
    """Return JWT `role` claim from a Supabase key, or None if not a standard JWT."""
    try:
        parts = key.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        pad = (4 - len(payload_b64) % 4) % 4
        payload_b64 += "=" * pad
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        r = data.get("role")
        return str(r) if r is not None else None
    except Exception:
        return None


def _append_github_env_multiline(name: str, value: str, path: str) -> None:
    if "\n" in value or "\r" in value:
        print("ERROR: token must not contain newlines", file=sys.stderr)
        sys.exit(1)
    delim = secrets.token_hex(16)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def _write_token_to_github_env(token: str, source: str) -> int:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        print("GITHUB_ENV not set; this script is intended for GitHub Actions.", file=sys.stderr)
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
        return _write_token_to_github_env(override, "environment BREEZE_SESSION_TOKEN")

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        print(
            "Add repository secret BREEZE_SESSION_TOKEN, or set SUPABASE_URL + SUPABASE_KEY for session_config.",
            file=sys.stderr,
        )
        return 1

    role = jwt_role_from_supabase_key(key)
    if role == "anon":
        print(
            "SUPABASE_KEY is the anon key; PostgREST returns no rows under RLS.\n"
            "Use the service_role key from Supabase → Project Settings → API,\n"
            "or add repository secret BREEZE_SESSION_TOKEN (daily Breeze token).",
            file=sys.stderr,
        )
        return 1

    client = create_client(url, key)
    res = client.table("session_config").select("breeze_session_token").limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        print(
            "session_config returned no rows. Fix one of:\n"
            "  1) Repository secret BREEZE_SESSION_TOKEN (Settings → Secrets → Actions).\n"
            "  2) Supabase SQL: sql/create_session_config.sql + non-empty breeze_session_token.\n"
            "  3) SUPABASE_KEY must be service_role (not anon).",
            file=sys.stderr,
        )
        return 1
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        print(
            "breeze_session_token is empty in Supabase; set secret BREEZE_SESSION_TOKEN or edit Table Editor.",
            file=sys.stderr,
        )
        return 1

    return _write_token_to_github_env(token, "Supabase session_config")


if __name__ == "__main__":
    raise SystemExit(main())
