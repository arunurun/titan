#!/usr/bin/env python3
"""One-off: scan 5 small-cap tickers and test breakout success email."""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT / "config" / ".env", override=False)

TIER = "SMALL_CAP_100"
TICKERS = [
    "NETWEB.NS",
    "OLAELEC.NS",
    "KAYNES.NS",
    "APOLLO.NS",
    "PARAS.NS",
]


def smtp_skip_reason() -> str | None:
    from email_notify import _smtp_config

    if _smtp_config():
        return None
    missing = [
        k
        for k in ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO")
        if not os.environ.get(k, "").strip()
    ]
    if missing:
        return f"Missing env: {', '.join(missing)}"
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip().replace(" ", "")
    if user and not password:
        return "SMTP_USER set but SMTP_PASSWORD empty"
    return "SMTP not configured (check email_notify logs)"


def main() -> None:
    from breakout_scanner import (
        SIGNAL_TIER_PASS,
        SIGNAL_TIER_WATCH,
        _build_breakout_email_body,
        _build_report_markdown,
        _send_breakout_success_email,
        evaluate_and_audit_stock,
        warm_yahoo_session,
    )

    scan_date = datetime.date.today()
    warm_yahoo_session()

    all_results: list[dict] = []

    def emit(line: str) -> None:
        print(line, flush=True)

    for ticker in TICKERS:
        candidate, _analysis = evaluate_and_audit_stock(ticker, TIER, emit=emit)
        if candidate:
            all_results.append(candidate)

    pass_count = sum(1 for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_PASS)
    watch_count = sum(1 for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_WATCH)

    report_markdown = _build_report_markdown(all_results, scan_date)
    email_sent = _send_breakout_success_email(
        scan_date=scan_date,
        tickers_scanned=len(TICKERS),
        all_results=all_results,
        report_markdown=report_markdown,
    )

    body = _build_breakout_email_body(
        scan_date=scan_date,
        tickers_scanned=len(TICKERS),
        all_results=all_results,
        report_markdown=report_markdown,
    )
    body_lines = body.splitlines()

    print("\n=== SUMMARY ===")
    print(f"tickers: {', '.join(TICKERS)}")
    print(f"candidate_count: {len(all_results)}")
    print(f"PASS: {pass_count}, WATCH: {watch_count}")
    print(f"email_sent: {email_sent}")
    if not email_sent:
        print(f"smtp_skip_reason: {smtp_skip_reason()}")
    print("\n=== EMAIL BODY (first 30 lines) ===")
    for line in body_lines[:30]:
        print(line)


if __name__ == "__main__":
    main()
