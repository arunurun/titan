"""Validate Breeze session token stored in Supabase session_config(id=1)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from email_notify import send_action_required_email


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _login_url(api_key: str) -> str:
    return f"https://api.icicidirect.com/apiuser/login?api_key={quote(api_key, safe='')}"


def _notify_token_issue(*, api_key: str, reason: str) -> None:
    login_url = _login_url(api_key)
    token_update_url = (os.environ.get("TOKEN_UPDATE_URL") or "").strip()
    detail_lines = [f"Reason: {reason}", f"Breeze login URL: {login_url}"]
    if token_update_url:
        detail_lines.append(f"Token update endpoint: {token_update_url}")
    send_action_required_email(
        "Breeze session token appears invalid or expired.",
        action_url=login_url,
        action_label="Login to Breeze",
        detail="\n".join(detail_lines),
        subject_prefix="Titan Breeze token validator",
    )


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
        _notify_token_issue(api_key=api_key, reason="No session_config row found for id=1.")
        return 2
    token = (rows[0].get("breeze_session_token") or "").strip()
    if not token:
        print("breeze_session_token is empty in session_config.")
        print(f"Breeze login URL: {_login_url(api_key)}")
        _notify_token_issue(api_key=api_key, reason="breeze_session_token is empty in session_config.")
        return 2

    breeze = BreezeConnect(api_key=api_key)
    try:
        breeze.generate_session(api_secret=api_secret, session_token=token)
    except Exception as exc:  # noqa: BLE001
        print("Breeze token is INVALID.")
        print(f"Reason: {exc}")
        print(f"Breeze login URL: {_login_url(api_key)}")
        _notify_token_issue(api_key=api_key, reason=str(exc))
        return 2

    print("Breeze token is VALID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
