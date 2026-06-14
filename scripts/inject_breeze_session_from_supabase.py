"""
Load BREEZE_SESSION_TOKEN into GITHUB_ENV for GitHub Actions (used by main.py --live).

Prefers Supabase session_config(id=1) when SUPABASE_URL + SUPABASE_KEY are set.
Falls back to env BREEZE_SESSION_TOKEN (e.g. repository secret) only when Supabase
is unavailable or returns an empty token.

Requires: SUPABASE_URL + SUPABASE_KEY (must be service_role JWT if RLS is on), table session_config.
"""

from __future__ import annotations

import base64
import hashlib
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


def _append_github_env_kv(name: str, value: str, path: str) -> None:
    if "\n" in value or "\r" in value:
        print(f"ERROR: {name} must not contain newlines", file=sys.stderr)
        sys.exit(1)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def _token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
    return f"len={len(token)} sha256={digest}"


def _write_token_to_github_env(token: str, source: str) -> int:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        print("GITHUB_ENV not set; this script is intended for GitHub Actions.", file=sys.stderr)
        return 1
    fingerprint = _token_fingerprint(token)
    _append_github_env_multiline("BREEZE_SESSION_TOKEN", token, gh_env)
    _append_github_env_kv("BREEZE_SESSION_TOKEN_SOURCE", source, gh_env)
    _append_github_env_kv("BREEZE_SESSION_TOKEN_FINGERPRINT", fingerprint, gh_env)
    print(
        "Injected BREEZE_SESSION_TOKEN into GITHUB_ENV "
        f"(source={source}, {fingerprint})."
    )
    return 0


def _read_session_token(client) -> tuple[str, bool]:
    res = client.table("session_config").select("breeze_session_token").eq("id", 1).limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        return "", False
    token = (data[0].get("breeze_session_token") or "").strip()
    return token, True


def _ensure_session_row(client) -> None:
    client.table("session_config").upsert({"id": 1, "breeze_session_token": ""}).execute()


def _load_token_from_supabase(url: str, key: str) -> tuple[str | None, str | None]:
    """Return (token, error_message). token is non-empty on success; error_message on hard failure."""
    role = jwt_role_from_supabase_key(key)
    if role == "anon":
        return None, (
            "SUPABASE_KEY is the anon key; PostgREST returns no rows under RLS.\n"
            "Use the service_role key from Supabase → Project Settings → API,\n"
            "or add repository secret BREEZE_SESSION_TOKEN (daily Breeze token)."
        )
    if role is None and key.startswith("sb_"):
        return None, (
            "SUPABASE_KEY appears to be a publishable key (sb_*), not service_role.\n"
            "Use the service_role key from Supabase -> Project Settings -> API."
        )

    client = create_client(url, key)
    token, has_row = _read_session_token(client)
    if not has_row:
        _ensure_session_row(client)
        token, has_row = _read_session_token(client)
    if not has_row:
        return None, (
            "session_config row id=1 not readable even after bootstrap.\n"
            "Verify SUPABASE_KEY is service_role and table public.session_config exists."
        )
    if not token:
        return "", None
    return token, None


def main() -> int:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        print("GITHUB_ENV not set; this script is intended for GitHub Actions.", file=sys.stderr)
        return 1

    env_fallback = (os.environ.get("BREEZE_SESSION_TOKEN") or "").strip()
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()

    if url and key:
        token, err = _load_token_from_supabase(url, key)
        if err:
            print(err, file=sys.stderr)
            return 1
        if token:
            return _write_token_to_github_env(token, "supabase:session_config(id=1)")
        print(
            "breeze_session_token is empty in Supabase session_config (id=1); "
            "falling back to env BREEZE_SESSION_TOKEN if set.",
            file=sys.stderr,
        )

    if env_fallback:
        return _write_token_to_github_env(env_fallback, "environment:BREEZE_SESSION_TOKEN")

    if not url or not key:
        print(
            "Set SUPABASE_URL + SUPABASE_KEY for session_config, "
            "or add repository secret BREEZE_SESSION_TOKEN.",
            file=sys.stderr,
        )
        return 1

    print(
        "breeze_session_token is empty in Supabase session_config (id=1); "
        "run workflow 'Persist Breeze Token (Manual)' with a fresh API_Session.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
