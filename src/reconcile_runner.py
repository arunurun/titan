from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from analysis_store import (
    build_reconcile_digest_lines,
    enrich_audits_with_stock_reconcile,
    persist_reconcile_backfill,
)
from config_loader import load_config
from email_notify import send_success_post_email

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@contextmanager
def _reconcile_mode_guard() -> Any:
    prev_reconcile_mode = os.environ.get("TITAN_RECONCILE_MODE")
    prev_report_only = os.environ.get("TITAN_RECONCILE_REPORT_ONLY")
    os.environ["TITAN_RECONCILE_MODE"] = "1"
    os.environ["TITAN_RECONCILE_REPORT_ONLY"] = "1"
    try:
        yield
    finally:
        if prev_reconcile_mode is None:
            os.environ.pop("TITAN_RECONCILE_MODE", None)
        else:
            os.environ["TITAN_RECONCILE_MODE"] = prev_reconcile_mode
        if prev_report_only is None:
            os.environ.pop("TITAN_RECONCILE_REPORT_ONLY", None)
        else:
            os.environ["TITAN_RECONCILE_REPORT_ONLY"] = prev_report_only


def _summarize_scope(*, all_stocks: bool, sector: str | None) -> str:
    if all_stocks:
        return "all-stocks"
    return str(sector or "").strip().lower() or "unknown"


def run_reconcile_report(
    *,
    sector: str | None,
    all_stocks: bool,
    backfill_days: int = 0,
    generate_report: bool = True,
    email_subject_prefix: str = "Titan V12.0 reconcile",
) -> dict[str, Any]:
    """
    Supabase-only reconciliation:
    - reads structured analytics tables
    - computes decision-efficacy report lines
    - sends report-only email output
    """
    with _reconcile_mode_guard():
        cfg = load_config()
        scope_label = _summarize_scope(all_stocks=all_stocks, sector=sector)
        summary: dict[str, Any] = {}
        digest_text = ""
        if generate_report:
            summary = enrich_audits_with_stock_reconcile(
                cfg,
                sector=None if all_stocks else scope_label,
                all_stocks=all_stocks,
                audits=[],
            )
            lines = [
                f"Titan EOD reconcile (Supabase-only): {scope_label}",
                f"Generated at: {datetime.now(IST).isoformat(timespec='seconds')}",
                "Guardrails: reconcile mode enabled; Breeze/market fetch blocked.",
                "",
            ]
            lines.extend(build_reconcile_digest_lines(summary))
            digest_text = "\n".join(lines).strip()
            send_success_post_email(digest_text, subject_prefix=email_subject_prefix)
        print("[reconcile] RECONCILE_MODE_SUPABASE_ONLY=1")
        print("[reconcile] TITAN_RECONCILE_MODE=1 TITAN_RECONCILE_REPORT_ONLY=1")
        print("[reconcile] Breeze guard active: market fetch calls blocked in reconcile mode")
        if digest_text:
            print(digest_text)

        backfill_meta = {"persisted": 0, "days": 0}
        if int(backfill_days or 0) > 0:
            backfill_meta = persist_reconcile_backfill(
                cfg,
                sector=None if all_stocks else scope_label,
                all_stocks=all_stocks,
                days=max(1, int(backfill_days)),
            )
            print(
                "[reconcile] backfill persisted="
                f"{backfill_meta.get('persisted', 0)} days={backfill_meta.get('days', 0)} "
                f"scope={scope_label}"
            )

        return {
            "scope": scope_label,
            "summary": summary,
            "digest_text": digest_text,
            "email_configured": bool((os.environ.get("SMTP_HOST") or "").strip())
            and bool((os.environ.get("EMAIL_TO") or "").strip()),
            "backfill": backfill_meta,
        }
