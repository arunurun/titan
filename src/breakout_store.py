"""Persist breakout scanner per-stock analysis rows to Supabase."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig
from json_util import sanitize_for_json

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_BREAKOUT_TABLE = "breakout_stock_analysis"
_UPSERT_CONFLICT = "run_id,ticker"


def _supabase_configured(cfg: TitanConfig) -> bool:
    return bool((cfg.supabase_url or "").strip() and (cfg.supabase_key or "").strip())


def build_analysis_record(
    *,
    run_id: str,
    scan_date: str | date,
    ticker: str,
    tier: str,
    symbol_yahoo: str,
    fetch_error: str | None = None,
    bar_count: int | None = None,
    latest_close: float | None = None,
    prev_close: float | None = None,
    pct_change: float | None = None,
    latest_volume: float | None = None,
    vol_20_avg: float | None = None,
    vol_mult: float | None = None,
    rsi_14: float | None = None,
    adx_14: float | None = None,
    sma_50: float | None = None,
    sma_200: float | None = None,
    poc_30d: float | None = None,
    min_price_threshold: float | None = None,
    vol_mult_threshold: float | None = None,
    price_above_sma50: bool | None = None,
    yahoo_as_of_date: str | None = None,
    passed: bool = False,
    fail_reason: str | None = None,
    entry_low: float | None = None,
    entry_high: float | None = None,
    stop_loss: float | None = None,
    target_price: float | None = None,
    target_gain_pct: float | None = None,
    signal_tier: str | None = None,
    persistence_score: int | None = None,
    composite_rank: float | None = None,
    liquidity_quality: float | None = None,
    breakout_stage: int | None = None,
    base_score: float | None = None,
    pass_paths: str | None = None,
    risk_flags: str | None = None,
    inserted_at: str | None = None,
) -> dict[str, Any]:
    """Map scanner internals to a flat row for ``breakout_stock_analysis``."""
    scan_iso = scan_date.isoformat() if isinstance(scan_date, date) else str(scan_date)
    sym = ticker.replace(".NS", "").strip()
    row: dict[str, Any] = {
        "run_id": run_id,
        "scan_date": scan_iso,
        "inserted_at": inserted_at or datetime.now(IST).isoformat(timespec="seconds"),
        "ticker": sym,
        "tier": tier,
        "symbol_yahoo": symbol_yahoo,
        "fetch_error": fetch_error,
        "bar_count": bar_count,
        "latest_close": latest_close,
        "prev_close": prev_close,
        "pct_change": pct_change,
        "latest_volume": latest_volume,
        "vol_20_avg": vol_20_avg,
        "vol_mult": vol_mult,
        "rsi_14": rsi_14,
        "adx_14": adx_14,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "poc_30d": poc_30d,
        "min_price_threshold": min_price_threshold,
        "vol_mult_threshold": vol_mult_threshold,
        "price_above_sma50": price_above_sma50,
        "yahoo_as_of_date": yahoo_as_of_date,
        "passed": bool(passed),
        "fail_reason": fail_reason,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "target_price": target_price,
        "target_gain_pct": target_gain_pct,
        "signal_tier": signal_tier,
        "persistence_score": persistence_score,
        "composite_rank": composite_rank,
        "liquidity_quality": liquidity_quality,
        "breakout_stage": breakout_stage,
        "base_score": base_score,
        "pass_paths": pass_paths,
        "risk_flags": risk_flags,
    }
    return sanitize_for_json(row)


def persist_breakout_stock_analysis(
    cfg: TitanConfig,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk upsert per-stock breakout analysis rows for one scanner run."""
    if not records:
        return {"configured": True, "persisted": False, "rows": 0, "reason": "no_records"}
    if not _supabase_configured(cfg):
        logger.warning(
            "Breakout analysis persist skipped: SUPABASE_URL and SUPABASE_KEY must be set."
        )
        return {"configured": False, "persisted": False, "rows": 0, "reason": "missing_config"}

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    payload = [sanitize_for_json(r) for r in records]
    try:
        client.table(_BREAKOUT_TABLE).upsert(payload, on_conflict=_UPSERT_CONFLICT).execute()
        return {"configured": True, "persisted": True, "rows": len(payload)}
    except APIError as e:
        err_payload = e.args[0] if e.args else {}
        code = err_payload.get("code", "") if isinstance(err_payload, dict) else ""
        msg = err_payload.get("message", str(e)) if isinstance(err_payload, dict) else str(e)
        if code == "PGRST205" or "could not find the table" in msg.lower():
            logger.warning(
                "Breakout analysis table missing; create public.%s in Supabase. Details: %s",
                _BREAKOUT_TABLE,
                msg,
            )
            return {"configured": True, "persisted": False, "rows": 0, "reason": "missing_tables"}
        logger.warning("Breakout analysis persist failed: %s", msg)
        return {"configured": True, "persisted": False, "rows": 0, "reason": "api_error", "message": msg}
    except Exception as e:  # pragma: no cover
        logger.warning("Breakout analysis persist failed: %s", e)
        return {"configured": True, "persisted": False, "rows": 0, "reason": "unexpected"}
