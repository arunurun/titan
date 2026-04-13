from __future__ import annotations

import os
import sys
from urllib.parse import quote

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from supabase import create_client

from notify import send_email_alert, send_webhook_alert


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def breeze_login_url(api_key: str) -> str:
    return f"https://api.icicidirect.com/apiuser/login?api_key={quote(api_key, safe='')}"


def load_token_from_supabase(url: str, supabase_key: str) -> str:
    client = create_client(url, supabase_key)
    res = client.table("session_config").select("breeze_session_token").eq("id", 1).limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        raise RuntimeError("No row found in session_config with id=1")
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        raise RuntimeError("breeze_session_token is empty in session_config")
    return token


def is_token_valid(api_key: str, api_secret: str, token: str) -> tuple[bool, str]:
    breeze = BreezeConnect(api_key=api_key)
    try:
        breeze.generate_session(api_secret=api_secret, session_token=token)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    load_dotenv()
    api_key = _required("BREEZE_API_KEY")
    api_secret = _required("BREEZE_SECRET")
    supabase_url = _required("SUPABASE_URL")
    supabase_key = _required("SUPABASE_KEY")
    token_update_url = (os.environ.get("TOKEN_UPDATE_URL") or "").strip()

    token = load_token_from_supabase(supabase_url, supabase_key)
    valid, reason = is_token_valid(api_key, api_secret, token)
    if valid:
        print("Breeze token is valid.")
        return 0

    login_url = breeze_login_url(api_key)
    body_lines = [
        "Breeze session token appears expired/invalid.",
        f"Reason: {reason}",
        "",
        f"Login URL: {login_url}",
    ]
    if token_update_url:
        body_lines.append(f"Token update endpoint: {token_update_url}")
    body = "\n".join(body_lines)

    send_webhook_alert(body)
    send_email_alert("Breeze token refresh required", body)
    print(body)
    return 2


if __name__ == "__main__":
    sys.exit(main())
