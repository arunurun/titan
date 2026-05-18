"""Persist compact run features and daily sector rollups to Supabase."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from datetime import date, datetime, timedelta
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


def _iso_date(d: date | datetime | None = None) -> str:
    if d is None:
        return datetime.now(IST).date().isoformat()
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d.isoformat()


def _parse_date(x: Any) -> date | None:
    s = str(x or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _mean_or_none(vals: list[float]) -> float | None:
    xs = [x for x in vals if not math.isnan(x)]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


def _median_or_none(vals: list[float]) -> float | None:
    xs = [x for x in vals if not math.isnan(x)]
    if not xs:
        return None
    return round(median(xs), 2)


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
        flags.append("panic-vol-down-day")
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
            "volume_participation_ratio": audit.get("volume_participation_ratio", audit.get("absorption_ratio")),
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
            "pct_volume_participation_gt_1": pct(
                lambda a: _safe_float(a.get("volume_participation_ratio", a.get("absorption_ratio"))) > 1.0
            ),
            "pct_absorption_gt_1": pct(
                lambda a: _safe_float(a.get("volume_participation_ratio", a.get("absorption_ratio"))) > 1.0
            ),
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
        # Replace same-day sector feature slice to avoid stale symbols leaking into
        # leaders/laggards when multiple runs happen on the same trade_date.
        client.table("symbol_daily_features").delete().eq("trade_date", trade_date).eq(
            "sector", sector
        ).execute()
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


def _rollup_from_daily_rows(
    rows: Sequence[dict[str, Any]],
    *,
    period_type: str,
    period_end: str,
    sector: str,
    window_days: int,
) -> dict[str, Any]:
    return sanitize_for_json(
        {
            "period_type": period_type,
            "period_end": period_end,
            "sector": sector,
            "window_days": int(window_days),
            "avg_intent_score": _mean_or_none([_safe_float(r.get("avg_intent_score")) for r in rows]),
            "avg_effective_intent_score": _mean_or_none(
                [_safe_float(r.get("avg_effective_intent_score")) for r in rows]
            ),
            "breadth_above_ema200_pct": _mean_or_none(
                [_safe_float(r.get("breadth_above_ema200_pct")) for r in rows]
            ),
            "pct_z_gt_2": _mean_or_none([_safe_float(r.get("pct_z_gt_2")) for r in rows]),
            "pct_absorption_gt_1": _mean_or_none(
                [_safe_float(r.get("pct_absorption_gt_1")) for r in rows]
            ),
            "trap_count": int(sum(int(r.get("trap_count") or 0) for r in rows)),
            "panic_absorption_count": int(
                sum(int(r.get("panic_absorption_count") or 0) for r in rows)
            ),
            "source_trade_days": len(rows),
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
        }
    )


def update_sector_period_rollups(
    cfg: TitanConfig,
    *,
    sector: str,
    as_of_date: date | str | None = None,
    lookback_days: int = 45,
) -> dict[str, Any]:
    if not analysis_store_enabled():
        return {"enabled": False, "updated": False}
    as_of = _parse_date(as_of_date) if isinstance(as_of_date, str) else as_of_date
    as_of = as_of or datetime.now(IST).date()
    start = as_of - timedelta(days=max(lookback_days, 31))
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("sector_daily_rollup")
            .select("*")
            .eq("sector", sector)
            .gte("trade_date", start.isoformat())
            .lte("trade_date", as_of.isoformat())
            .order("trade_date")
            .execute()
        )
        rows = list(getattr(res, "data", None) or [])
        if not rows:
            return {"enabled": True, "updated": False, "reason": "no_daily_rows"}

        dated_rows: list[tuple[date, dict[str, Any]]] = []
        for r in rows:
            d = _parse_date(r.get("trade_date"))
            if d is not None:
                dated_rows.append((d, r))
        if not dated_rows:
            return {"enabled": True, "updated": False, "reason": "no_parseable_dates"}

        to_upsert: list[dict[str, Any]] = []
        for days in (7, 15, 30):
            cutoff = as_of - timedelta(days=days - 1)
            window_rows = [r for d, r in dated_rows if cutoff <= d <= as_of]
            to_upsert.append(
                _rollup_from_daily_rows(
                    window_rows,
                    period_type=f"{days}d",
                    period_end=as_of.isoformat(),
                    sector=sector,
                    window_days=days,
                )
            )

        iso_year, iso_week, _ = as_of.isocalendar()
        weekly_rows = [r for d, r in dated_rows if d.isocalendar()[:2] == (iso_year, iso_week)]
        to_upsert.append(
            _rollup_from_daily_rows(
                weekly_rows,
                period_type="weekly",
                period_end=as_of.isoformat(),
                sector=sector,
                window_days=len(weekly_rows),
            )
        )
        monthly_rows = [r for d, r in dated_rows if (d.year, d.month) == (as_of.year, as_of.month)]
        to_upsert.append(
            _rollup_from_daily_rows(
                monthly_rows,
                period_type="monthly",
                period_end=as_of.isoformat(),
                sector=sector,
                window_days=len(monthly_rows),
            )
        )
        client.table("sector_period_rollup").upsert(
            to_upsert, on_conflict="period_type,period_end,sector"
        ).execute()
        return {"enabled": True, "updated": True, "rows": len(to_upsert)}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        logger.warning("Period rollup update failed: %s", msg)
        return {"enabled": True, "updated": False, "reason": "api_error"}


def _trend_word(delta: float | None) -> str:
    if delta is None or math.isnan(delta):
        return "n/a"
    if delta > 1.0:
        return "improving"
    if delta < -1.0:
        return "deteriorating"
    return "stable"


def build_comparison_payload(
    cfg: TitanConfig,
    *,
    sector: str,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    if not analysis_store_enabled():
        return {"enabled": False}
    as_of = _parse_date(as_of_date) if isinstance(as_of_date, str) else as_of_date
    as_of = as_of or datetime.now(IST).date()
    as_of_s = as_of.isoformat()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        today_res = (
            client.table("sector_daily_rollup")
            .select("*")
            .eq("sector", sector)
            .eq("trade_date", as_of_s)
            .limit(1)
            .execute()
        )
        today_rows = list(getattr(today_res, "data", None) or [])
        today = today_rows[0] if today_rows else {}

        period_res = (
            client.table("sector_period_rollup")
            .select("*")
            .eq("sector", sector)
            .eq("period_end", as_of_s)
            .in_("period_type", ["7d", "15d", "30d", "weekly", "monthly"])
            .execute()
        )
        period_rows = list(getattr(period_res, "data", None) or [])
        by_period = {str(r.get("period_type")): r for r in period_rows}

        sym_res = (
            client.table("symbol_daily_features")
            .select("symbol,effective_intent_score,z_score,absorption_ratio,flags,return_1d_pct")
            .eq("sector", sector)
            .eq("trade_date", as_of_s)
            .order("effective_intent_score", desc=True)
            .limit(8)
            .execute()
        )
        leaders = list(getattr(sym_res, "data", None) or [])
        lag_res = (
            client.table("symbol_daily_features")
            .select("symbol,effective_intent_score,z_score,absorption_ratio,flags,return_1d_pct")
            .eq("sector", sector)
            .eq("trade_date", as_of_s)
            .order("effective_intent_score", desc=False)
            .limit(8)
            .execute()
        )
        laggards = list(getattr(lag_res, "data", None) or [])

        today_eff = _safe_float(today.get("avg_effective_intent_score"))
        d7 = _safe_float(by_period.get("7d", {}).get("avg_effective_intent_score"))
        d30 = _safe_float(by_period.get("30d", {}).get("avg_effective_intent_score"))
        delta_7 = None if math.isnan(today_eff) or math.isnan(d7) else round(today_eff - d7, 2)
        delta_30 = None if math.isnan(today_eff) or math.isnan(d30) else round(today_eff - d30, 2)

        risk_flags = int(today.get("trap_count") or 0) + int(today.get("panic_absorption_count") or 0)
        regime = "neutral"
        if (not math.isnan(today_eff) and today_eff >= 60.0) and risk_flags <= 1:
            regime = "bullish"
        elif (not math.isnan(today_eff) and today_eff <= 45.0) or risk_flags >= 4:
            regime = "defensive"

        return sanitize_for_json(
            {
                "enabled": True,
                "sector": sector,
                "as_of_date": as_of_s,
                "regime": regime,
                "today": today,
                "period_rollups": by_period,
                "delta": {
                    "avg_effective_intent_vs_7d": delta_7,
                    "avg_effective_intent_vs_30d": delta_30,
                    "trend_7d": _trend_word(delta_7),
                    "trend_30d": _trend_word(delta_30),
                },
                "leaders": leaders[:5],
                "laggards": laggards[:5],
            }
        )
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        logger.warning("Comparison payload build failed: %s", msg)
        return {"enabled": True, "error": "api_error"}


def quality_checks_for_run(
    audits: Sequence[dict[str, Any]],
    *,
    comparison: dict[str, Any] | None = None,
) -> list[str]:
    warnings: list[str] = []
    if len(audits) < 5:
        warnings.append("low_symbol_coverage")
    if any(int(a.get("rows") or 0) < 20 for a in audits):
        warnings.append("short_history_for_some_symbols")
    if comparison and comparison.get("enabled"):
        today = comparison.get("today") or {}
        if int(today.get("symbol_count") or 0) < len(audits):
            warnings.append("rollup_symbol_count_mismatch")
    return warnings


def persist_llm_digest_memory(
    cfg: TitanConfig,
    *,
    run_id: str,
    sector: str,
    prompt_facts: dict[str, Any],
    output_text: str,
    model_name: str | None = None,
    full_digest: str | None = None,
    github_run_id: str | None = None,
) -> dict[str, Any]:
    if not analysis_store_enabled():
        return {"enabled": False, "persisted": False}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    row_body: dict[str, Any] = {
        "run_id": run_id,
        "sector": sector,
        "prompt_facts": prompt_facts,
        "output_text": output_text,
        "model_name": model_name or "",
        "output_chars": len(output_text or ""),
        "recorded_at": datetime.now(IST).isoformat(timespec="seconds"),
    }
    gh = (github_run_id or "").strip()
    if gh:
        row_body["github_run_id"] = gh
    if full_digest is not None:
        row_body["full_digest"] = full_digest
    row = sanitize_for_json(row_body)
    try:
        client.table("llm_digest_memory").upsert(row, on_conflict="run_id").execute()
        return {"enabled": True, "persisted": True}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        logger.warning("LLM digest memory persist failed: %s", msg)
        return {"enabled": True, "persisted": False, "reason": "api_error"}
