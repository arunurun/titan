"""Persist compact run features and daily sector rollups to Supabase."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from collections import defaultdict
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


VALID_ACTION_SIGNALS: tuple[str, ...] = ("buy", "hold", "trim", "exit-risk")
DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS = 30
ONE_WEEK_TRADING_DAYS = 5
ONE_MONTH_TRADING_DAYS = 20


def _normalize_action_signal(raw_signal: Any) -> str:
    text_signal = str(raw_signal or "").strip().lower()
    if text_signal in ("exit", "exit_risk", "exitrisk", "exit-risk"):
        return "exit-risk"
    if text_signal in VALID_ACTION_SIGNALS:
        return text_signal
    return "hold"


def _signal_consistency_ratios_from_sequence(signal_sequence: Sequence[str]) -> dict[str, float]:
    total_points = len(signal_sequence)
    if total_points <= 0:
        return {
            "buy_signal_consistency_ratio": 0.0,
            "hold_signal_consistency_ratio": 0.0,
            "trim_signal_consistency_ratio": 0.0,
            "exit_risk_signal_consistency_ratio": 0.0,
        }
    return {
        "buy_signal_consistency_ratio": round(signal_sequence.count("buy") / total_points, 4),
        "hold_signal_consistency_ratio": round(signal_sequence.count("hold") / total_points, 4),
        "trim_signal_consistency_ratio": round(signal_sequence.count("trim") / total_points, 4),
        "exit_risk_signal_consistency_ratio": round(signal_sequence.count("exit-risk") / total_points, 4),
    }


def _evaluate_transition_horizon_outcome(
    target_signal: str,
    realized_return_pct: float | None,
) -> str | None:
    if realized_return_pct is None or math.isnan(realized_return_pct):
        return None
    outcome_threshold_pct = 0.25
    is_bullish_signal = target_signal in ("buy", "hold")
    if is_bullish_signal:
        if realized_return_pct >= outcome_threshold_pct:
            return "favorable"
        if realized_return_pct <= -outcome_threshold_pct:
            return "unfavorable"
        return "neutral"
    if realized_return_pct <= -outcome_threshold_pct:
        return "favorable"
    if realized_return_pct >= outcome_threshold_pct:
        return "unfavorable"
    return "neutral"


def _compounded_return_pct_for_horizon(
    trailing_rows_after_transition: Sequence[dict[str, Any]],
    *,
    required_trading_days: int,
) -> float | None:
    if len(trailing_rows_after_transition) < required_trading_days:
        return None
    growth_multiplier = 1.0
    for feature_row in trailing_rows_after_transition[:required_trading_days]:
        daily_return_pct = _safe_float(feature_row.get("return_1d_pct"))
        if math.isnan(daily_return_pct):
            return None
        growth_multiplier *= 1.0 + (daily_return_pct / 100.0)
    return round((growth_multiplier - 1.0) * 100.0, 4)


def build_stock_signal_transition_analytics_row(
    *,
    sector: str,
    symbol: str,
    exchange: str,
    run_id: str,
    as_of_trade_date: date,
    historical_feature_rows: Sequence[dict[str, Any]],
    trailing_window_days: int = DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS,
) -> dict[str, Any]:
    parsed_rows: list[dict[str, Any]] = []
    for feature_row in historical_feature_rows:
        trade_day = _parse_date(feature_row.get("trade_date"))
        if trade_day is None or trade_day > as_of_trade_date:
            continue
        normalized_signal = _normalize_action_signal(feature_row.get("action_signal"))
        parsed_rows.append(
            {
                **feature_row,
                "_parsed_trade_date": trade_day,
                "_normalized_action_signal": normalized_signal,
            }
        )
    if not parsed_rows:
        return sanitize_for_json(
            {
                "trade_date": as_of_trade_date.isoformat(),
                "sector": sector,
                "symbol": symbol,
                "exchange": exchange,
                "run_id": run_id,
                "trailing_window_days": int(trailing_window_days),
                "previous_signal": None,
                "current_signal": "hold",
                "transition_type": "insufficient_history",
                "transition_date": None,
                "days_in_previous_signal": None,
                "buy_signal_consistency_ratio": 0.0,
                "hold_signal_consistency_ratio": 0.0,
                "trim_signal_consistency_ratio": 0.0,
                "exit_risk_signal_consistency_ratio": 0.0,
                "transition_stability_score": 0.0,
                "is_whipsaw_transition": False,
                "whipsaw_transition_count": 0,
                "transition_event_count": 0,
                "matured_1w_available": False,
                "matured_1w_realized_return_pct": None,
                "matured_1w_outcome": None,
                "matured_1m_available": False,
                "matured_1m_realized_return_pct": None,
                "matured_1m_outcome": None,
                "computed_at": datetime.now(IST).isoformat(timespec="seconds"),
            }
        )

    parsed_rows.sort(key=lambda row: row["_parsed_trade_date"])
    trailing_cutoff_date = as_of_trade_date - timedelta(days=max(1, int(trailing_window_days)) - 1)
    trailing_rows = [row for row in parsed_rows if row["_parsed_trade_date"] >= trailing_cutoff_date]
    if not trailing_rows:
        trailing_rows = [parsed_rows[-1]]

    transition_events: list[dict[str, Any]] = []
    signal_segments: list[dict[str, Any]] = []
    for row in trailing_rows:
        row_signal = row["_normalized_action_signal"]
        row_date = row["_parsed_trade_date"]
        if not signal_segments:
            signal_segments.append(
                {
                    "signal": row_signal,
                    "start_date": row_date,
                    "end_date": row_date,
                    "trade_day_count": 1,
                }
            )
            continue
        active_segment = signal_segments[-1]
        if active_segment["signal"] == row_signal:
            active_segment["end_date"] = row_date
            active_segment["trade_day_count"] += 1
            continue
        transition_events.append(
            {
                "from_signal": active_segment["signal"],
                "to_signal": row_signal,
                "date": row_date,
            }
        )
        signal_segments.append(
            {
                "signal": row_signal,
                "start_date": row_date,
                "end_date": row_date,
                "trade_day_count": 1,
            }
        )

    current_segment = signal_segments[-1]
    previous_segment = signal_segments[-2] if len(signal_segments) >= 2 else None
    current_signal = str(current_segment["signal"])
    previous_signal = str(previous_segment["signal"]) if previous_segment is not None else None
    transition_date = current_segment["start_date"]
    if previous_signal and previous_signal != current_signal:
        transition_type = f"{previous_signal}_to_{current_signal}"
    elif previous_signal:
        transition_type = "no_transition"
    else:
        transition_type = "initial_observation"

    whipsaw_transition_count = 0
    is_whipsaw_transition = False
    for transition_index in range(1, len(transition_events)):
        previous_event = transition_events[transition_index - 1]
        current_event = transition_events[transition_index]
        is_reversal = (
            previous_event["from_signal"] == current_event["to_signal"]
            and previous_event["to_signal"] == current_event["from_signal"]
        )
        day_gap = (current_event["date"] - previous_event["date"]).days
        if is_reversal and day_gap <= 5:
            whipsaw_transition_count += 1
            if transition_index == len(transition_events) - 1:
                is_whipsaw_transition = True

    consistency_ratios = _signal_consistency_ratios_from_sequence(
        [str(row["_normalized_action_signal"]) for row in trailing_rows]
    )
    dominant_consistency_ratio = max(consistency_ratios.values()) if consistency_ratios else 0.0
    transition_event_count = len(transition_events)
    transition_stability_score = max(
        0.0,
        min(
            100.0,
            round(
                (20.0 + (65.0 * dominant_consistency_ratio))
                - (5.0 * transition_event_count)
                - (15.0 * whipsaw_transition_count),
                2,
            ),
        ),
    )

    rows_after_transition = [
        row for row in parsed_rows if row["_parsed_trade_date"] > transition_date
    ]
    matured_1w_realized_return_pct = _compounded_return_pct_for_horizon(
        rows_after_transition,
        required_trading_days=ONE_WEEK_TRADING_DAYS,
    )
    matured_1m_realized_return_pct = _compounded_return_pct_for_horizon(
        rows_after_transition,
        required_trading_days=ONE_MONTH_TRADING_DAYS,
    )
    matured_1w_outcome = _evaluate_transition_horizon_outcome(
        current_signal,
        matured_1w_realized_return_pct,
    )
    matured_1m_outcome = _evaluate_transition_horizon_outcome(
        current_signal,
        matured_1m_realized_return_pct,
    )

    return sanitize_for_json(
        {
            "trade_date": as_of_trade_date.isoformat(),
            "sector": sector,
            "symbol": symbol,
            "exchange": exchange,
            "run_id": run_id,
            "trailing_window_days": int(trailing_window_days),
            "previous_signal": previous_signal,
            "current_signal": current_signal,
            "transition_type": transition_type,
            "transition_date": transition_date.isoformat(),
            "days_in_previous_signal": (
                int(previous_segment["trade_day_count"]) if previous_segment is not None else None
            ),
            **consistency_ratios,
            "transition_stability_score": transition_stability_score,
            "is_whipsaw_transition": bool(is_whipsaw_transition),
            "whipsaw_transition_count": int(whipsaw_transition_count),
            "transition_event_count": int(transition_event_count),
            "matured_1w_available": matured_1w_realized_return_pct is not None,
            "matured_1w_realized_return_pct": matured_1w_realized_return_pct,
            "matured_1w_outcome": matured_1w_outcome,
            "matured_1m_available": matured_1m_realized_return_pct is not None,
            "matured_1m_realized_return_pct": matured_1m_realized_return_pct,
            "matured_1m_outcome": matured_1m_outcome,
            "computed_at": datetime.now(IST).isoformat(timespec="seconds"),
        }
    )


def persist_stock_signal_transition_analytics(
    cfg: TitanConfig,
    *,
    client: Any,
    sector: str,
    run_id: str,
    as_of_trade_date: date,
    audits: Sequence[dict[str, Any]],
    trailing_window_days: int = DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS,
) -> dict[str, Any]:
    symbol_pairs = sorted(
        {
            (str(a.get("symbol") or "").strip(), str(a.get("exchange") or "").strip())
            for a in audits
            if str(a.get("symbol") or "").strip() and str(a.get("exchange") or "").strip()
        }
    )
    if not symbol_pairs:
        return {"persisted": False, "reason": "no_symbols"}

    lookback_start_date = as_of_trade_date - timedelta(
        days=max(60, int(trailing_window_days) + ONE_MONTH_TRADING_DAYS + 10)
    )
    transition_rows: list[dict[str, Any]] = []
    for symbol, exchange in symbol_pairs:
        history_response = (
            client.table("symbol_daily_features")
            .select("trade_date,symbol,exchange,action_signal,return_1d_pct")
            .eq("sector", sector)
            .eq("symbol", symbol)
            .eq("exchange", exchange)
            .gte("trade_date", lookback_start_date.isoformat())
            .lte("trade_date", as_of_trade_date.isoformat())
            .order("trade_date")
            .execute()
        )
        historical_rows = list(getattr(history_response, "data", None) or [])
        transition_rows.append(
            build_stock_signal_transition_analytics_row(
                sector=sector,
                symbol=symbol,
                exchange=exchange,
                run_id=run_id,
                as_of_trade_date=as_of_trade_date,
                historical_feature_rows=historical_rows,
                trailing_window_days=trailing_window_days,
            )
        )

    client.table("stock_signal_transition_analytics").upsert(
        transition_rows,
        on_conflict="trade_date,sector,symbol,exchange,trailing_window_days",
    ).execute()
    return {"persisted": True, "rows": len(transition_rows)}


def build_stock_transition_validation_checks(*, sector: str, trade_date: str) -> dict[str, str]:
    return {
        "presence_check": (
            "select trade_date, sector, count(*) as row_count "
            "from public.stock_signal_transition_analytics "
            f"where sector = '{sector}' and trade_date = '{trade_date}' "
            "group by trade_date, sector;"
        ),
        "maturity_check": (
            "select current_signal, matured_1w_available, matured_1m_available, count(*) as symbols "
            "from public.stock_signal_transition_analytics "
            f"where sector = '{sector}' and trade_date = '{trade_date}' "
            "group by current_signal, matured_1w_available, matured_1m_available "
            "order by current_signal, matured_1w_available desc, matured_1m_available desc;"
        ),
        "whipsaw_check": (
            "select symbol, exchange, transition_type, whipsaw_transition_count, "
            "transition_stability_score "
            "from public.stock_signal_transition_analytics "
            f"where sector = '{sector}' and trade_date = '{trade_date}' and whipsaw_transition_count > 0 "
            "order by whipsaw_transition_count desc, transition_stability_score asc "
            "limit 20;"
        ),
    }


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
    if audit.get("high_volume_down_day_proxy") or audit.get("panic_absorption_proxy"):
        flags.append("high-vol-down-day")
    if audit.get("cluster_guardrail_applied"):
        flags.append("cluster-guardrail")
    if audit.get("macro_guardrail_applied"):
        flags.append("macro-guardrail")
    if audit.get("event_guardrail_applied") or audit.get("event_risk_soon"):
        flags.append("event-guardrail")

    tape_extras = {
        "return_5d_pct": audit.get("return_5d_pct"),
        "return_10d_pct": audit.get("return_10d_pct"),
        "return_20d_pct": audit.get("return_20d_pct"),
        "rel_return_5d_vs_nifty_pct": audit.get("rel_return_5d_vs_nifty_pct"),
        "rel_return_10d_vs_nifty_pct": audit.get("rel_return_10d_vs_nifty_pct"),
        "rel_return_20d_vs_nifty_pct": audit.get("rel_return_20d_vs_nifty_pct"),
        "median_notional_inr_20d": audit.get("median_notional_inr_20d"),
        "liquidity_thin_proxy": audit.get("liquidity_thin_proxy"),
        "extreme_price_move_proxy": audit.get("extreme_price_move_proxy"),
        "atr_penalty_input": audit.get("atr_penalty_input"),
        "sector_pctile_effective_intent": audit.get("sector_pctile_effective_intent"),
        "sector_pctile_next_week_score": audit.get("sector_pctile_next_week_score"),
        "sector_pctile_return_5d_pct": audit.get("sector_pctile_return_5d_pct"),
        "next_week_score": audit.get("next_week_score"),
        "next_day_score": audit.get("next_day_score"),
        "prediction_breakdown": audit.get("prediction_breakdown"),
        "sell_signal": audit.get("sell_signal"),
        "reconcile_next_day_hit": audit.get("reconcile_next_day_hit"),
        "reconcile_next_week_hit": audit.get("reconcile_next_week_hit"),
        "reconcile_transition_quality": audit.get("reconcile_transition_quality"),
        "reconcile_signal_transition": audit.get("reconcile_signal_transition"),
        "reconcile_news_summary": audit.get("reconcile_news_summary"),
        "news_correlation": audit.get("news_correlation"),
    }

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
            "action_signal": _normalize_action_signal(
                audit.get("action_signal", audit.get("sell_signal"))
            ),
            "z_score": audit.get("z_score"),
            "volume_participation_ratio": audit.get("volume_participation_ratio", audit.get("absorption_ratio")),
            "absorption_ratio": audit.get("absorption_ratio"),
            "return_1d_pct": audit.get("return_1d_pct"),
            "ema_200_distance_pct": audit.get("ema_200_distance_pct"),
            "atr_14_pct": audit.get("atr_14_pct"),
            "flags": flags,
            "option_chain_unavailable": bool(audit.get("option_chain_unavailable", False)),
            "rows_count": int(audit.get("rows") or 0),
            "tape_extras": tape_extras,
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
            "panic_absorption_count": sum(
                1
                for a in audits
                if a.get("high_volume_down_day_proxy") or a.get("panic_absorption_proxy")
            ),
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
        transition_persist_meta = persist_stock_signal_transition_analytics(
            cfg,
            client=client,
            sector=sector,
            run_id=run_id,
            as_of_trade_date=run_ts.date(),
            audits=audits,
            trailing_window_days=DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS,
        )

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
            "transition_rows": int(transition_persist_meta.get("rows") or 0),
            "transition_validation_checks": build_stock_transition_validation_checks(
                sector=sector,
                trade_date=trade_date,
            ),
        }
    except APIError as e:
        payload = e.args[0] if e.args else {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        if code == "PGRST205" or "could not find the table" in msg.lower():
            logger.warning(
                "Analysis store tables missing; run sql/create_analysis_rollups.sql and relevant alter migrations. Details: %s",
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


def _score_direction(score: Any) -> str:
    v = _safe_float(score)
    if math.isnan(v):
        return "unknown"
    if v >= 55.0:
        return "up"
    if v <= 45.0:
        return "down"
    return "neutral"


def _return_direction(ret_pct: Any) -> str:
    v = _safe_float(ret_pct)
    if math.isnan(v):
        return "unknown"
    if v >= 0.3:
        return "up"
    if v <= -0.3:
        return "down"
    return "neutral"


def _direction_hit(predicted: str, realized: str) -> bool | None:
    if predicted == "unknown" or realized == "unknown":
        return None
    if predicted == "neutral":
        return realized == "neutral"
    return predicted == realized


def _transition_quality(prev_score: Any, curr_score: Any) -> str:
    prev = _safe_float(prev_score)
    curr = _safe_float(curr_score)
    if math.isnan(prev) or math.isnan(curr):
        return "unknown"
    delta = curr - prev
    if delta >= 3.0:
        return "improving"
    if delta <= -3.0:
        return "deteriorating"
    return "stable"


def _news_summary_from_audit(audit: dict[str, Any]) -> str:
    corr = audit.get("news_correlation")
    if not isinstance(corr, dict):
        return "news: n/a"
    driver = str(corr.get("driver") or "n/a").strip()
    metric = str(corr.get("affected_metric") or "n/a").strip()
    direction = str(corr.get("direction") or "neutral").strip()
    conf = _safe_float(corr.get("confidence"))
    conf_txt = "n/a" if math.isnan(conf) else f"{conf:.2f}"
    return f"news: {direction} via {driver} on {metric} (conf {conf_txt})"


def _safe_tape_extras(row: dict[str, Any]) -> dict[str, Any]:
    x = row.get("tape_extras")
    return x if isinstance(x, dict) else {}


def build_stock_reconcile_snapshot(
    audits: Sequence[dict[str, Any]],
    *,
    historical_rows_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    accuracy: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {"next_day": {"hit": 0, "total": 0}, "next_week": {"hit": 0, "total": 0}}
    )
    transition_counts: dict[str, int] = defaultdict(int)
    per_symbol: dict[str, dict[str, Any]] = {}
    news_rows: list[dict[str, Any]] = []

    for audit in audits:
        symbol = str(audit.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        signal = _normalize_action_signal(audit.get("action_signal", audit.get("sell_signal")))
        hist = historical_rows_by_symbol.get(symbol, [])
        prev = hist[0] if hist else {}
        prev_tape = _safe_tape_extras(prev) if isinstance(prev, dict) else {}
        week_ref = hist[4] if len(hist) >= 5 else {}
        week_tape = _safe_tape_extras(week_ref) if isinstance(week_ref, dict) else {}

        pred_1d = _score_direction(prev_tape.get("next_day_score"))
        realized_1d = _return_direction(audit.get("return_1d_pct"))
        hit_1d = _direction_hit(pred_1d, realized_1d)

        pred_1w = _score_direction(week_tape.get("next_week_score"))
        realized_1w = _return_direction(audit.get("return_5d_pct"))
        hit_1w = _direction_hit(pred_1w, realized_1w)

        if hit_1d is not None:
            accuracy[signal]["next_day"]["total"] += 1
            accuracy[signal]["next_day"]["hit"] += int(hit_1d)
        if hit_1w is not None:
            accuracy[signal]["next_week"]["total"] += 1
            accuracy[signal]["next_week"]["hit"] += int(hit_1w)

        prev_signal = _normalize_action_signal(
            (prev if isinstance(prev, dict) else {}).get("action_signal", prev_tape.get("sell_signal"))
        )
        curr_week = audit.get("next_week_score")
        prev_week = prev_tape.get("next_week_score")
        q = _transition_quality(prev_week, curr_week)
        transition_counts[q] += 1
        signal_transition = f"{prev_signal}->{signal}"
        news_summary = _news_summary_from_audit(audit)

        row = {
            "symbol": symbol,
            "signal": signal,
            "pred_next_day": pred_1d,
            "realized_next_day": realized_1d,
            "hit_next_day": hit_1d,
            "pred_next_week": pred_1w,
            "realized_next_week": realized_1w,
            "hit_next_week": hit_1w,
            "signal_transition": signal_transition,
            "transition_quality": q,
            "news_summary": news_summary,
        }
        per_symbol[symbol] = row
        news_rows.append(
            {
                "symbol": symbol,
                "signal": signal,
                "summary": news_summary,
            }
        )

    coverage_1d = sum(v["next_day"]["total"] for v in accuracy.values())
    coverage_1w = sum(v["next_week"]["total"] for v in accuracy.values())
    acc_json = {k: v for k, v in accuracy.items()}
    transition_json = {k: int(v) for k, v in transition_counts.items()}
    return sanitize_for_json(
        {
            "symbol_count": len(per_symbol),
            "coverage_next_day": coverage_1d,
            "coverage_next_week": coverage_1w,
            "accuracy_by_signal_horizon": acc_json,
            "transition_quality": transition_json,
            "news_attribution_summaries": news_rows[:20],
            "per_symbol": per_symbol,
        }
    )


def _fetch_symbol_history_by_sector(
    cfg: TitanConfig,
    *,
    sector: str,
    lookback_days: int = 45,
) -> dict[str, list[dict[str, Any]]]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    start = (datetime.now(IST).date() - timedelta(days=max(lookback_days, 10))).isoformat()
    try:
        res = (
            client.table("symbol_daily_features")
            .select("trade_date,symbol,action_signal,tape_extras")
            .eq("sector", sector)
            .gte("trade_date", start)
            .order("trade_date", desc=True)
            .execute()
        )
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile history fetch failed for %s: %s", sector, e)
        return {}
    rows = list(getattr(res, "data", None) or [])
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out[symbol].append(row)
    return out


def build_reconcile_digest_lines(summary: dict[str, Any]) -> list[str]:
    lines = ["--- Stock-level reconcile ---"]
    lines.append(
        f"Coverage: symbols={int(summary.get('symbol_count') or 0)} | "
        f"next-day={int(summary.get('coverage_next_day') or 0)} | "
        f"next-week={int(summary.get('coverage_next_week') or 0)}"
    )
    acc = summary.get("accuracy_by_signal_horizon")
    if isinstance(acc, dict):
        for signal in ("buy", "hold", "trim", "exit-risk"):
            row = acc.get(signal) if isinstance(acc.get(signal), dict) else {}
            d1 = row.get("next_day") if isinstance(row, dict) else {}
            d7 = row.get("next_week") if isinstance(row, dict) else {}
            d1_hit = int((d1 or {}).get("hit") or 0)
            d1_total = int((d1 or {}).get("total") or 0)
            d7_hit = int((d7 or {}).get("hit") or 0)
            d7_total = int((d7 or {}).get("total") or 0)
            d1_pct = round((100.0 * d1_hit / d1_total), 1) if d1_total else None
            d7_pct = round((100.0 * d7_hit / d7_total), 1) if d7_total else None
            lines.append(
                f"{signal}: 1D {d1_hit}/{d1_total} ({d1_pct if d1_pct is not None else 'n/a'}%) | "
                f"1W {d7_hit}/{d7_total} ({d7_pct if d7_pct is not None else 'n/a'}%)"
            )
    tq = summary.get("transition_quality")
    if isinstance(tq, dict):
        lines.append(
            "Transition quality: "
            f"improving={int(tq.get('improving') or 0)}, "
            f"stable={int(tq.get('stable') or 0)}, "
            f"deteriorating={int(tq.get('deteriorating') or 0)}"
        )
    news = summary.get("news_attribution_summaries")
    if isinstance(news, list) and news:
        lines.append("News attribution highlights:")
        for row in news[:8]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"{str(row.get('symbol') or '').upper()} [{str(row.get('signal') or 'hold').lower()}] "
                f"{str(row.get('summary') or 'news: n/a')}"
            )
    return lines


def enrich_audits_with_stock_reconcile(
    cfg: TitanConfig,
    *,
    sector: str,
    audits: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    history = _fetch_symbol_history_by_sector(cfg, sector=sector)
    summary = build_stock_reconcile_snapshot(audits, historical_rows_by_symbol=history)
    per_symbol = summary.get("per_symbol")
    if not isinstance(per_symbol, dict):
        return summary
    for audit in audits:
        symbol = str(audit.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        row = per_symbol.get(symbol)
        if not isinstance(row, dict):
            continue
        audit["reconcile_next_day_hit"] = row.get("hit_next_day")
        audit["reconcile_next_week_hit"] = row.get("hit_next_week")
        audit["reconcile_signal_transition"] = row.get("signal_transition")
        audit["reconcile_transition_quality"] = row.get("transition_quality")
        audit["reconcile_news_summary"] = row.get("news_summary")
    return summary


def persist_reconcile_backfill(
    cfg: TitanConfig,
    *,
    sector: str,
    days: int,
) -> dict[str, Any]:
    if days <= 0:
        return {"persisted": 0, "days": 0}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        rows_res = (
            client.table("symbol_daily_features")
            .select("trade_date,symbol,sector,action_signal,return_1d_pct,tape_extras")
            .eq("sector", sector)
            .order("trade_date", desc=True)
            .limit(max(60, days * 20))
            .execute()
        )
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile backfill fetch failed: %s", e)
        return {"persisted": 0, "days": days, "reason": "fetch_failed"}
    rows = [r for r in list(getattr(rows_res, "data", None) or []) if isinstance(r, dict)]
    if not rows:
        return {"persisted": 0, "days": days, "reason": "no_rows"}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        td = str(row.get("trade_date") or "").strip()
        sym = str(row.get("symbol") or "").strip().upper()
        if not td or not sym:
            continue
        by_date[td].append(row)
        by_symbol[sym].append(row)
    trade_dates = sorted(by_date.keys(), reverse=True)[:days]
    persisted = 0
    for td in trade_dates:
        audits: list[dict[str, Any]] = []
        hist: dict[str, list[dict[str, Any]]] = {}
        for row in by_date.get(td, []):
            sym = str(row.get("symbol") or "").strip().upper()
            tape = _safe_tape_extras(row)
            audits.append(
                {
                    "symbol": sym,
                    "action_signal": row.get("action_signal"),
                    "sell_signal": tape.get("sell_signal"),
                    "next_week_score": tape.get("next_week_score"),
                    "return_1d_pct": row.get("return_1d_pct"),
                    "return_5d_pct": tape.get("return_5d_pct"),
                    "news_correlation": tape.get("news_correlation"),
                }
            )
            hist[sym] = [x for x in by_symbol.get(sym, []) if str(x.get("trade_date")) < td]
        summary = build_stock_reconcile_snapshot(audits, historical_rows_by_symbol=hist)
        lines = build_reconcile_digest_lines(summary)
        run_id = f"reconcile-backfill-{sector}-{td}"
        payload = sanitize_for_json(
            {
                "run_id": run_id,
                "sector": sector,
                "prompt_facts": {"trade_date": td, "reconcile_summary": summary},
                "output_text": "\n".join(lines),
                "full_digest": "\n".join(lines),
                "model_name": "reconcile_backfill",
                "output_chars": len("\n".join(lines)),
                "recorded_at": datetime.now(IST).isoformat(timespec="seconds"),
            }
        )
        try:
            client.table("llm_digest_memory").upsert(payload, on_conflict="run_id").execute()
            persisted += 1
        except Exception as e:  # pragma: no cover
            logger.warning("Reconcile backfill upsert failed for %s/%s: %s", sector, td, e)
    return {"persisted": persisted, "days": len(trade_dates)}


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
        return {"enabled": True, "persisted": False, "reason": "api_error", "message": msg}
