"""Parse, validate, and persist a Breeze API_Session token to Supabase session_config."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from breeze_session_auth import parse_api_session_from_input  # noqa: E402


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def main() -> int:
    load_dotenv(override=False)
    raw = (os.environ.get("BREEZE_TOKEN_INPUT") or "").strip()
    if not raw:
        raise RuntimeError("Missing BREEZE_TOKEN_INPUT. Pass API_Session or redirect URL.")

    api_key = _required("BREEZE_API_KEY")
    api_secret = _required("BREEZE_SECRET")
    supabase_url = _required("SUPABASE_URL")
    supabase_key = _required("SUPABASE_KEY")

    token = parse_api_session_from_input(raw)
    breeze = BreezeConnect(api_key=api_key)
    breeze.generate_session(api_secret=api_secret, session_token=token)

    client = create_client(supabase_url, supabase_key)
    client.table("session_config").upsert(
        {"id": 1, "breeze_session_token": token},
        on_conflict="id",
    ).execute()
    print("Token validated and persisted to Supabase session_config(id=1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
