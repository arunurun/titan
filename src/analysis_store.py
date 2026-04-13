"""Persist compact run features and daily sector rollups to Supabase."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from datetime import datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig
from json_util import sanitize_for_json

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def analysis_store_enabled() -> bool:
    """Feature gate for incremental rollout."""
    return _env_truthy("TITAN_ENABLE_ANALYSIS_STORE", default=False)


def build_symbol_daily_feature(
    audit: dict[str, Any],
    *,
    trade_date: str,
    sector: str,
    run_id: str,
    run_ts_iso: str,
) -> dict[str, Any]:
    flags: list[str] = []
    if audit.get("trap_exit_proxy"):
        flags.append("up-move-trap")
    if audit.get("panic_absorption_proxy"):
        flags.append("panic-absorption")
    if audit.get("cluster_guardrail_applied"):
        flags.append("cluster-guardrail")
    if audit.get("macro_guardrail_applied"):
        flags.append("macro-guardrail")
    if audit.get("event_guardrail_applied") or audit.get("event_risk_soon"):
        flags.append("event-guardrail")

    return sanitize_for_json(
        {
            "trade_date": trade_date,
            "sector": sector,
            "symbol": audit.get("symbol"),
            "exchange": audit.get("exchange"),
            "run_id": run_id,
            "run_ts": run_ts_iso,
            "intent_score": audit.get("intent_score"),
            "effective_intent_score": audit.get("effective_intent_score", audit.get("intent_score")),
            "z_score": audit.get("z_score"),
            "absorption_ratio": audit.get("absorption_ratio"),
            "return_1d_pct": audit.get("return_1d_pct"),
            "ema_200_distance_pct": audit.get("ema_200_distance_pct"),
            "atr_14_pct": audit.get("atr_14_pct"),
            "flags": flags,
            "option_chain_unavailable": bool(audit.get("option_chain_unavailable", False)),
            "rows_count": int(audit.get("rows") or 0),
        }
    )


def build_sector_daily_rollup(
    audits: Sequence[dict[str, Any]],
    *,
    trade_date: str,
    sector: str,
    run_id: str,
    run_ts_iso: str,
) -> dict[str, Any]:
    n = len(audits)
    intents = [_safe_float(a.get("intent_score")) for a in audits]
    intents = [x for x in intents if not math.isnan(x)]
    eff = [_safe_float(a.get("effective_intent_score", a.get("intent_score"))) for a in audits]
    eff = [x for x in eff if not math.isnan(x)]

    def pct(pred) -> float | None:
        if n == 0:
            return None
        return round(100.0 * sum(1 for a in audits if pred(a)) / n, 2)

    return sanitize_for_json(
        {
            "trade_date": trade_date,
            "sector": sector,
            "run_id": run_id,
            "run_ts": run_ts_iso,
            "symbol_count": n,
            "avg_intent_score": (round(sum(intents) / len(intents), 2) if intents else None),
            "median_intent_score": (round(median(intents), 2) if intents else None),
            "avg_effective_intent_score": (round(sum(eff) / len(eff), 2) if eff else None),
            "breadth_above_ema200_pct": pct(
                lambda a: _safe_float(a.get("ema_200_distance_pct")) > 0.0
            ),
            "pct_z_gt_2": pct(lambda a: _safe_float(a.get("z_score")) > 2.0),
            "pct_absorption_gt_1": pct(lambda a: _safe_float(a.get("absorption_ratio")) > 1.0),
            "trap_count": sum(1 for a in audits if a.get("trap_exit_proxy")),
            "panic_absorption_count": sum(1 for a in audits if a.get("panic_absorption_proxy")),
            "macro_guardrail_count": sum(1 for a in audits if a.get("macro_guardrail_applied")),
            "cluster_guardrail_count": sum(1 for a in audits if a.get("cluster_guardrail_applied")),
            "event_guardrail_count": sum(1 for a in audits if a.get("event_guardrail_applied")),
        }
    )


def persist_sector_run_analytics(
    cfg: TitanConfig,
    *,
    sector: str,
    audits: Sequence[dict[str, Any]],
    mode: str,
    ok_count: int,
    total_count: int,
) -> dict[str, Any]:
    if not analysis_store_enabled():
        return {"enabled": False, "persisted": False}
    if not audits:
        return {"enabled": True, "persisted": False, "reason": "no_audits"}

    run_ts = datetime.now(IST)
    run_ts_iso = run_ts.isoformat(timespec="seconds")
    trade_date = run_ts.date().isoformat()
    run_id = f"{sector}-{run_ts.strftime('%Y%m%d-%H%M%S')}"

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        client.table("run_metadata").upsert(
            sanitize_for_json(
                {
                    "run_id": run_id,
                    "run_ts": run_ts_iso,
                    "trade_date": trade_date,
                    "sector": sector,
                    "mode": mode,
                    "status": "completed" if ok_count > 0 else "failed",
                    "symbol_count": int(total_count),
                    "ok_count": int(ok_count),
                    "meta": {"source": "titan-sector-live"},
                }
            ),
            on_conflict="run_id",
        ).execute()

        features = [
            build_symbol_daily_feature(
                a, trade_date=trade_date, sector=sector, run_id=run_id, run_ts_iso=run_ts_iso
            )
            for a in audits
        ]
        client.table("symbol_daily_features").upsert(
            features,
            on_conflict="trade_date,sector,symbol,exchange",
        ).execute()

        rollup = build_sector_daily_rollup(
            list(audits),
            trade_date=trade_date,
            sector=sector,
            run_id=run_id,
            run_ts_iso=run_ts_iso,
        )
        client.table("sector_daily_rollup").upsert(
            rollup,
            on_conflict="trade_date,sector",
        ).execute()
        return {
            "enabled": True,
            "persisted": True,
            "run_id": run_id,
            "feature_rows": len(features),
        }
    except APIError as e:
        payload = e.args[0] if e.args else {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        if code == "PGRST205" or "could not find the table" in msg.lower():
            logger.warning(
                "Analysis store tables missing; run sql/create_analysis_rollups.sql. Details: %s",
                msg,
            )
            return {"enabled": True, "persisted": False, "reason": "missing_tables"}
        logger.warning("Analysis store persist failed: %s", msg)
        return {"enabled": True, "persisted": False, "reason": "api_error"}
    except Exception as e:  # pragma: no cover
        logger.warning("Analysis store persist failed: %s", e)
        return {"enabled": True, "persisted": False, "reason": "unexpected"}
