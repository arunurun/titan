"""Helpers to capture and persist ICICI Breeze API_Session (semi-automated).

ICICI requires browser login + OTP; there is no supported fully headless flow.
See: https://www.icicidirect.com/ilearn/stocks/articles/how-to-generate-session-key
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


def build_breeze_login_url(api_key: str) -> str:
    """Login URL per ICICI docs: api_key must be URL-encoded."""
    encoded = quote(api_key, safe="")
    return f"https://api.icicidirect.com/apiuser/login?api_key={encoded}"


def parse_api_session_from_input(raw: str) -> str:
    """
    Accept:
    - Full redirect URL containing API_Session query param
    - Raw token string (alphanumeric / mixed from ICICI)
    """
    text = raw.strip().strip('"').strip("'")
    if not text:
        raise ValueError("Empty input")

    if "://" in text or text.startswith("http"):
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        for key in ("API_Session", "apisession", "api_session", "session_token"):
            if key in qs and qs[key]:
                return qs[key][0].strip()
        # ICICI sometimes uses apisession=... (case variants)
        for k, vals in qs.items():
            if not vals:
                continue
            kl = k.lower()
            if kl in ("apisession", "api_session", "session_token"):
                return vals[0].strip()
        raise ValueError("No API_Session (or apisession) found in URL query string")

    return text


def upsert_env_var(env_path: Path, name: str, value: str) -> None:
    """Create or update NAME=value in a .env file; preserve other lines."""
    line_re = re.compile(rf"^{re.escape(name)}=")
    new_line = f"{name}={value}\n"

    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []

    out: list[str] = []
    replaced = False
    for line in lines:
        if line_re.match(line):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)

    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(new_line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("".join(out), encoding="utf-8")
