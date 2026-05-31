#!/usr/bin/env python3
"""Preflight news cache checks before a Titan run.

Checks macro ``global_news_snapshots`` freshness and optionally per-symbol
``news_feed`` recency for a sector (priority list or full universe).

Exit 0 with warnings by default (fail-open). Set ``TITAN_NEWS_PREFLIGHT_STRICT=1``
to exit 1 when checks fail.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config_loader import TitanConfig
from news_config import prepare_news_script_config
from sector_priority import _load_latest_news_snapshot, _news_snapshot_ttl_hours, load_priority_instruments
from sector_registry import load_sector_instruments

try:
    from news_store import get_recent_news_for_symbol
except ImportError:
    get_recent_news_for_symbol = None  # type: ignore[misc, assignment]


def _strict_mode() -> bool:
    return str(os.environ.get("TITAN_NEWS_PREFLIGHT_STRICT", "")).strip() == "1"


def _news_max_age_hours() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", "")) or "").strip()
    if not raw:
        return 36.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 36.0


def _parse_utc(raw: str | None) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_global_snapshot(cfg: TitanConfig, *, now_utc: datetime | None = None) -> dict:
    """Return global_news_snapshots freshness status."""
    now = now_utc or datetime.now(timezone.utc)
    ttl_hours = _news_snapshot_ttl_hours()
    row = _load_latest_news_snapshot(cfg)
    if not row:
        return {
            "ok": False,
            "level": "error",
            "message": "No global_news_snapshots row found",
            "refreshed_at": None,
            "age_minutes": None,
            "ttl_hours": ttl_hours,
            "item_count": 0,
            "fetch_status": "missing",
        }
    refreshed_at = _parse_utc(str(row.get("refreshed_at") or ""))
    if refreshed_at is None:
        return {
            "ok": False,
            "level": "error",
            "message": "Latest global_news_snapshots row has invalid refreshed_at",
            "refreshed_at": row.get("refreshed_at"),
            "age_minutes": None,
            "ttl_hours": ttl_hours,
            "item_count": int(row.get("item_count") or 0),
            "fetch_status": str(row.get("fetch_status") or "unknown"),
        }
    age_minutes = max(0.0, (now - refreshed_at).total_seconds() / 60.0)
    fresh = age_minutes <= (ttl_hours * 60.0)
    item_count = int(row.get("item_count") or 0)
    fetch_status = str(row.get("fetch_status") or "ok")
    if fresh and item_count > 0 and fetch_status not in ("error", "empty"):
        return {
            "ok": True,
            "level": "ok",
            "message": f"Global snapshot fresh ({age_minutes:.0f}m old, ttl={ttl_hours}h, items={item_count})",
            "refreshed_at": refreshed_at.isoformat(),
            "age_minutes": round(age_minutes, 1),
            "ttl_hours": ttl_hours,
            "item_count": item_count,
            "fetch_status": fetch_status,
        }
    if fresh and item_count == 0:
        return {
            "ok": False,
            "level": "warning",
            "message": f"Global snapshot fresh but empty (age={age_minutes:.0f}m, status={fetch_status})",
            "refreshed_at": refreshed_at.isoformat(),
            "age_minutes": round(age_minutes, 1),
            "ttl_hours": ttl_hours,
            "item_count": item_count,
            "fetch_status": fetch_status,
        }
    return {
        "ok": False,
        "level": "warning" if row else "error",
        "message": (
            f"Global snapshot stale or empty (age={age_minutes:.0f}m, ttl={ttl_hours}h, "
            f"items={item_count}, status={fetch_status})"
        ),
        "refreshed_at": refreshed_at.isoformat(),
        "age_minutes": round(age_minutes, 1),
        "ttl_hours": ttl_hours,
        "item_count": item_count,
        "fetch_status": fetch_status,
    }


def _resolve_sector_symbols(
    cfg: TitanConfig,
    sector_id: str,
    *,
    priority_only: bool,
    priority_top_n: int | None,
) -> list[tuple[str, str]]:
    sector_key = str(sector_id or "").strip().lower()
    if not sector_key:
        return []
    instruments = []
    if priority_only:
        instruments = load_priority_instruments(cfg, sector_key=sector_key, top_n=priority_top_n)
        if not instruments:
            instruments = load_sector_instruments(sector_key)
    else:
        instruments = load_sector_instruments(sector_key)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for inst in instruments:
        key = (inst.symbol.strip().upper(), inst.exchange.strip().upper())
        if not key[0] or key in seen or key[1] not in ("NSE", "BSE"):
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def check_sector_symbol_news(
    cfg: TitanConfig,
    sector_id: str,
    *,
    priority_only: bool = False,
    priority_top_n: int | None = None,
    now_utc: datetime | None = None,
) -> dict | None:
    """Optional news_feed recency check for sector symbols."""
    if not sector_id.strip():
        return None
    if get_recent_news_for_symbol is None:
        return {
            "ok": False,
            "level": "warning",
            "message": "news_store unavailable; symbol news recency skipped",
            "sector_id": sector_id,
            "symbols_checked": 0,
            "symbols_with_news": 0,
            "symbols_stale_or_missing": 0,
            "max_age_hours": _news_max_age_hours(),
        }
    pairs = _resolve_sector_symbols(
        cfg,
        sector_id,
        priority_only=priority_only,
        priority_top_n=priority_top_n,
    )
    if not pairs:
        return {
            "ok": False,
            "level": "warning",
            "message": f"No symbols resolved for sector={sector_id!r}",
            "sector_id": sector_id,
            "symbols_checked": 0,
            "symbols_with_news": 0,
            "symbols_stale_or_missing": 0,
            "max_age_hours": _news_max_age_hours(),
        }
    now = now_utc or datetime.now(timezone.utc)
    max_age = _news_max_age_hours()
    cutoff = now - timedelta(hours=max_age)
    with_news = 0
    stale_or_missing = 0
    sample_missing: list[str] = []
    for symbol, exchange in pairs:
        rows = get_recent_news_for_symbol(
            cfg,
            symbol,
            exchange,
            lookback_hours=int(max_age),
            limit=1,
        )
        if not rows:
            stale_or_missing += 1
            if len(sample_missing) < 5:
                sample_missing.append(symbol)
            continue
        published = _parse_utc(str(rows[0].get("published_at") or ""))
        if published is None or published < cutoff:
            stale_or_missing += 1
            if len(sample_missing) < 5:
                sample_missing.append(symbol)
        else:
            with_news += 1
    checked = len(pairs)
    ok = stale_or_missing == 0
    scope = "priority" if priority_only else "full"
    if ok:
        msg = (
            f"Sector {sector_id} ({scope}): all {checked} symbols have news within {max_age:.0f}h"
        )
    else:
        msg = (
            f"Sector {sector_id} ({scope}): {stale_or_missing}/{checked} symbols missing recent news "
            f"(within {max_age:.0f}h)"
        )
        if sample_missing:
            msg += f"; examples: {', '.join(sample_missing)}"
    return {
        "ok": ok,
        "level": "ok" if ok else "warning",
        "message": msg,
        "sector_id": sector_id,
        "symbols_checked": checked,
        "symbols_with_news": with_news,
        "symbols_stale_or_missing": stale_or_missing,
        "max_age_hours": max_age,
    }


def run_preflight(
    cfg: TitanConfig,
    *,
    sector_id: str = "",
    priority_only: bool = False,
    priority_top_n: int | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Run all preflight checks; return structured report."""
    global_status = check_global_snapshot(cfg, now_utc=now_utc)
    sector_status = None
    if sector_id.strip():
        sector_status = check_sector_symbol_news(
            cfg,
            sector_id,
            priority_only=priority_only,
            priority_top_n=priority_top_n,
            now_utc=now_utc,
        )
    failures = [global_status] if not global_status.get("ok") else []
    if sector_status and not sector_status.get("ok"):
        failures.append(sector_status)
    return {
        "global": global_status,
        "sector": sector_status,
        "failures": failures,
        "strict": _strict_mode(),
    }


def _print_report(report: dict) -> None:
    print("=== Titan news preflight ===")
    global_status = report.get("global") or {}
    print(f"[global] {global_status.get('level', 'unknown').upper()}: {global_status.get('message')}")
    sector_status = report.get("sector")
    if sector_status:
        print(f"[sector] {sector_status.get('level', 'unknown').upper()}: {sector_status.get('message')}")
    failures = list(report.get("failures") or [])
    if failures:
        print(f"Warnings/failures: {len(failures)}")
        if report.get("strict"):
            print("Strict mode enabled (TITAN_NEWS_PREFLIGHT_STRICT=1); exiting non-zero.")
        else:
            print("Fail-open mode: continuing with warnings (set TITAN_NEWS_PREFLIGHT_STRICT=1 to fail).")
    else:
        print("All preflight checks passed.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight news cache before Titan run.")
    parser.add_argument(
        "--sector",
        default="",
        help="Optional sector id for symbol news_feed recency check.",
    )
    parser.add_argument(
        "--priority-only",
        action="store_true",
        help="When --sector is set, check priority list symbols only.",
    )
    parser.add_argument(
        "--priority-top-n",
        type=int,
        default=None,
        help="Top N priority symbols when --priority-only is set.",
    )
    args = parser.parse_args()

    try:
        cfg = prepare_news_script_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1 if _strict_mode() else 0

    report = run_preflight(
        cfg,
        sector_id=str(args.sector or "").strip(),
        priority_only=bool(args.priority_only),
        priority_top_n=args.priority_top_n,
    )
    _print_report(report)
    failures = list(report.get("failures") or [])
    if failures and report.get("strict"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
