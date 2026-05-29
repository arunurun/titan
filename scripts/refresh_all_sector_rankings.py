"""Refresh sector priority rankings + daily winners for every active sector (excl. unknown).

Used by the Saturday scheduled workflow. Runs each sector via the same logic as
``refresh_sector_daily_winners.py`` (Breeze-heavy).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from breeze_connect import BreezeConnect

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from breeze_session_auth import validate_breeze_session_token

HEARTBEAT_SECONDS = 60.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _validate_token_shape(token_raw: str, *, min_length: int = 8) -> str:
    return validate_breeze_session_token(token_raw, min_length=min_length)


def _preflight_breeze_session() -> None:
    token_source = str(os.environ.get("BREEZE_SESSION_TOKEN_SOURCE") or "env:BREEZE_SESSION_TOKEN").strip()
    token_raw = os.environ.get("BREEZE_SESSION_TOKEN", "")
    try:
        token = _validate_token_shape(token_raw)
    except ValueError as exc:
        token_len = len(str(token_raw or "").strip())
        raise RuntimeError(
            "Breeze session preflight failed before sector fan-out: "
            f"{exc}. Refresh session_config(id=1) with a valid API_Session and rerun. "
            f"diagnostics={{source:{token_source},len:{token_len}}}"
        ) from exc

    token_len = len(token)
    token_fp = _token_fingerprint(token)
    api_key = _required_env("BREEZE_API_KEY")
    api_secret = _required_env("BREEZE_SECRET")

    breeze = BreezeConnect(api_key=api_key)
    try:
        # Single lightweight auth call; if this fails we abort before sector loop.
        breeze.generate_session(api_secret=api_secret, session_token=token)
    except Exception as exc:  # noqa: BLE001
        reason = type(exc).__name__
        raise RuntimeError(
            "Breeze session preflight failed before sector fan-out: token invalid/expired. "
            "Update session_config(id=1) with a fresh API_Session and rerun. "
            f"diagnostics={{source:{token_source},len:{token_len},fp:{token_fp}}} "
            f"reason_type={reason}"
        ) from exc

    print(
        f"[{_utc_now_iso()}] Breeze preflight OK diagnostics={{source:{token_source},len:{token_len},fp:{token_fp}}}",
        flush=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Refresh rankings for all sectors")
    p.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Winners / priority depth per sector (default 10)",
    )
    p.add_argument(
        "--exclude-sectors",
        type=str,
        default="unknown,non_equity",
        help="Comma-separated sector_key values to skip",
    )
    args = p.parse_args()
    top_n = max(1, int(args.top_n))
    excl = {x.strip().lower() for x in str(args.exclude_sectors).split(",") if x.strip()}

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    from sector_registry import list_active_sector_ids

    _preflight_breeze_session()

    sectors = [s for s in list_active_sector_ids(include_unknown=False) if s not in excl]
    sectors.sort()
    if not sectors:
        print(json.dumps({"error": "no_sectors", "exclude": sorted(excl)}, indent=2))
        return 1

    script = ROOT / "scripts" / "refresh_sector_daily_winners.py"
    results: list[dict[str, object]] = []
    failed: list[str] = []

    for idx, sector in enumerate(sectors, start=1):
        sector_started = time.perf_counter()
        print(
            f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] sector={sector} started top_n={top_n}",
            flush=True,
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--sector",
                sector,
                "--top-n",
                str(top_n),
            ],
            cwd=ROOT,
            text=True,
        )
        next_heartbeat = time.perf_counter() + HEARTBEAT_SECONDS
        while proc.poll() is None:
            now = time.perf_counter()
            if now >= next_heartbeat:
                elapsed = now - sector_started
                print(
                    f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] heartbeat sector={sector} elapsed_s={elapsed:.1f}",
                    flush=True,
                )
                next_heartbeat = now + HEARTBEAT_SECONDS
            time.sleep(1.0)
        exit_code = int(proc.returncode or 0)
        ok = exit_code == 0
        if not ok:
            failed.append(sector)
        elapsed_total = time.perf_counter() - sector_started
        print(
            f"[{_utc_now_iso()}] [{idx}/{len(sectors)}] sector={sector} finished exit_code={exit_code} elapsed_s={elapsed_total:.1f}",
            flush=True,
        )
        results.append(
            {
                "sector": sector,
                "ok": ok,
                "exit_code": exit_code,
                "elapsed_seconds": round(elapsed_total, 2),
            }
        )

    summary = {
        "top_n": top_n,
        "exclude": sorted(excl),
        "sector_count": len(sectors),
        "failed": sorted(failed),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
