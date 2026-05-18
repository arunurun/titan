"""Emit BREEZE_API_KEY from .env for piping to: npx wrangler secret put BREEZE_API_KEY

Usage (from repo root, Worker name from wrangler.toml = titan-proxy):

  python scripts/emit_breeze_api_key_for_wrangler.py | npx wrangler secret put BREEZE_API_KEY

Do not commit .env; this writes the key to stdout only when run locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    from dotenv import load_dotenv
    import os

    load_dotenv(ROOT / ".env", override=False)
    key = (os.environ.get("BREEZE_API_KEY") or "").strip()
    if not key:
        print("BREEZE_API_KEY missing or empty in .env", file=sys.stderr)
        return 2
    sys.stdout.write(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
