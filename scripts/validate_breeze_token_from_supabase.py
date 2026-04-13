"""Validate Breeze session token stored in Supabase session_config(id=1)."""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from supabase import create_client


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _login_url(api_key: str) -> str:
    return f"https://api.icicidirect.com/apiuser/login?api_key={quote(api_key, safe='')}"


def main() -> int:
    load_dotenv(override=False)
    api_key = _required("BREEZE_API_KEY")
    api_secret = _required("BREEZE_SECRET")
    supabase_url = _required("SUPABASE_URL")
    supabase_key = _required("SUPABASE_KEY")

    client = create_client(supabase_url, supabase_key)
    res = (
        client.table("session_config")
        .select("breeze_session_token")
        .eq("id", 1)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        print("No session_config row found for id=1.")
        print(f"Breeze login URL: {_login_url(api_key)}")
        return 2
    token = (rows[0].get("breeze_session_token") or "").strip()
    if not token:
        print("breeze_session_token is empty in session_config.")
        print(f"Breeze login URL: {_login_url(api_key)}")
        return 2

    breeze = BreezeConnect(api_key=api_key)
    try:
        breeze.generate_session(api_secret=api_secret, session_token=token)
    except Exception as exc:  # noqa: BLE001
        print("Breeze token is INVALID.")
        print(f"Reason: {exc}")
        print(f"Breeze login URL: {_login_url(api_key)}")
        return 2

    print("Breeze token is VALID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
