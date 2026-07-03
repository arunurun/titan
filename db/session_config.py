"""Supabase session_config helpers for Breeze API session token."""

from __future__ import annotations

import os

from supabase import create_client


def _supabase_client():
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")
    return create_client(url, key)


def get_breeze_token() -> str:
    """Return breeze_session_token from session_config row id=1."""
    client = _supabase_client()
    res = (
        client.table("session_config")
        .select("breeze_session_token")
        .eq("id", 1)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        raise RuntimeError("No row found in session_config with id=1")
    token = (rows[0].get("breeze_session_token") or "").strip()
    if not token:
        raise RuntimeError("breeze_session_token is empty in session_config")
    return token
