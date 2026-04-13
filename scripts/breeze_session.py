#!/usr/bin/env python3
"""
Semi-automated Breeze session capture.

ICICI does not support unattended OAuth/OTP. This script:
1) Opens the official login URL (or prints it with --no-browser)
2) After you log in, paste the API_Session from DevTools Network Form Data,
   or paste the full redirect URL containing API_Session
3) Validates via Breeze SDK generate_session (same as runtime)
4) Writes BREEZE_SESSION_TOKEN to your .env

Usage (from project root):
  python scripts/breeze_session.py
  python scripts/breeze_session.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from breeze_connect import BreezeConnect  # noqa: E402
from breeze_session_auth import (  # noqa: E402
    build_breeze_login_url,
    parse_api_session_from_input,
    upsert_env_var,
)


def _load_breeze_creds() -> tuple[str, str]:
    load_dotenv(ROOT / ".env", override=False)
    key = os.environ.get("BREEZE_API_KEY", "").strip()
    secret = os.environ.get("BREEZE_SECRET", "").strip()
    if not key or not secret:
        raise SystemExit(
            "Set BREEZE_API_KEY and BREEZE_SECRET in .env (copy from config/.env.example)."
        )
    return key, secret


def _validate_session(api_key: str, api_secret: str, api_session: str) -> None:
    breeze = BreezeConnect(api_key=api_key)
    breeze.generate_session(api_secret=api_secret, session_token=api_session)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Breeze API_Session into .env")
    parser.add_argument("--no-browser", action="store_true", help="Only print login URL")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="Path to .env to update (default: project .env)",
    )
    args = parser.parse_args()

    api_key, api_secret = _load_breeze_creds()
    url = build_breeze_login_url(api_key)
    print("1) Log in with your ICICI credentials (OTP as required).")
    print("2) From DevTools Network, copy Form Data API_Session, or copy the full redirect URL.")
    print()
    print(url)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"(Could not open browser: {e})")

    raw = input("\nPaste API_Session or redirect URL here: ").strip()
    try:
        api_session = parse_api_session_from_input(raw)
    except ValueError as e:
        raise SystemExit(str(e)) from e

    print("Validating session with Breeze SDK...")
    try:
        _validate_session(api_key, api_secret, api_session)
    except Exception as e:
        raise SystemExit(f"Validation failed: {e}") from e

    upsert_env_var(args.env_file, "BREEZE_SESSION_TOKEN", api_session)
    print(f"Updated {args.env_file} with BREEZE_SESSION_TOKEN.")
    print("Session keys typically expire within 24h or at midnight (ICICI); re-run this script when calls fail.")


if __name__ == "__main__":
    main()
