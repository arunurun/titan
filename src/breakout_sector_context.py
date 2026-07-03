"""Sector leadership context for breakout composite ranking (read-only Supabase)."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_SRR_META_KEYS = (
    "sector_relative_rank_score",
    "rank_score",
    "sector_pctile_next_week_score",
)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    return val


def _score_from_meta(meta: dict[str, Any] | None) -> float | None:
    if not isinstance(meta, dict):
        return None
    for key in _SRR_META_KEYS:
        val = _safe_float(meta.get(key))
        if val is not None:
            return val
    rank_pct = _safe_float(meta.get("rank_percentile"))
    if rank_pct is not None:
        return rank_pct
    return None


def _supabase_client() -> Any | None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    return create_client(url, key)


def load_sector_lead_scores(
    symbols: list[str],
    as_of_date: str | None = None,
) -> dict[str, float]:
    """
    Max sector leadership score per symbol across active sector mappings.

    Reads ``sector_priority_rankings.meta`` (``sector_relative_rank_score`` or
  rank percentile). Symbols without data are omitted; callers treat missing as
    neutral 50 in ``composite_rank_score``.
    """
    syms = sorted({str(s).strip().upper() for s in symbols if s})
    if not syms:
        return {}

    as_of = as_of_date or datetime.now(IST).date().isoformat()
    client = _supabase_client()
    if client is None:
        return {}

    scores: dict[str, list[float]] = defaultdict(list)

    def _ingest_rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if sym not in syms:
                continue
            srr = _score_from_meta(row.get("meta"))
            if srr is not None:
                scores[sym].append(srr)

    try:
        res = (
            client.table("sector_priority_rankings")
            .select("symbol,exchange,meta,as_of_date")
            .in_("symbol", syms)
            .eq("as_of_date", as_of)
            .execute()
        )
        _ingest_rows(list(getattr(res, "data", None) or []))
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return {}
        logger.info("sector_priority_rankings read failed: %s", exc)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.info("sector_priority_rankings read failed: %s", exc)
        return {}

    if not scores:
        try:
            res = (
                client.table("sector_priority_rankings")
                .select("symbol,exchange,meta,as_of_date")
                .in_("symbol", syms)
                .lte("as_of_date", as_of)
                .order("as_of_date", desc=True)
                .limit(max(200, len(syms) * 5))
                .execute()
            )
            seen: set[str] = set()
            for row in list(getattr(res, "data", None) or []):
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "").strip().upper()
                if sym in seen or sym not in syms:
                    continue
                srr = _score_from_meta(row.get("meta"))
                if srr is not None:
                    scores[sym].append(srr)
                    seen.add(sym)
        except Exception as exc:  # noqa: BLE001
            logger.info("sector_priority_rankings fallback read failed: %s", exc)

    return {sym: round(max(vals), 2) for sym, vals in scores.items() if vals}
