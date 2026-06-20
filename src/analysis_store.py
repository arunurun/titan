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


def forward_return_eval_enabled() -> bool:
    """When set, shortlist efficacy scores FORWARD (+1/+5) returns vs same-day."""
    return _env_truthy("TITAN_FORWARD_RETURN_EVAL", default=False)


def forward_outcomes_persist_enabled() -> bool:
    """When set, persist +1/+5 forward outcomes and post-signal drawdown into tape_extras."""
    return _env_truthy("TITAN_FORWARD_OUTCOMES_PERSIST", default=False)


_DEFAULT_FORWARD_OUTCOME_HORIZONS: tuple[int, ...] = (1, 5)
_FORWARD_OUTCOME_LOOKBACK_ENV = "TITAN_FORWARD_OUTCOMES_LOOKBACK_DAYS"
_DEFAULT_FORWARD_OUTCOME_LOOKBACK_DAYS = 90


def _forward_outcome_lookback_days() -> int:
    raw = os.environ.get(_FORWARD_OUTCOME_LOOKBACK_ENV, "").strip()
    if not raw:
        return _DEFAULT_FORWARD_OUTCOME_LOOKBACK_DAYS
    try:
        return max(7, int(raw))
    except ValueError:
        return _DEFAULT_FORWARD_OUTCOME_LOOKBACK_DAYS


def _import_forward_return_eval() -> Any:
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    scripts_str = str(scripts_dir)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)
    import forward_return_eval  # noqa: PLC0415

    return forward_return_eval


def _existing_forward_outcomes(tape_extras: Any) -> dict[str, Any]:
    if not isinstance(tape_extras, dict):
        return {}
    existing = tape_extras.get("forward_outcomes")
    return dict(existing) if isinstance(existing, dict) else {}


def _forward_outcomes_need_update(existing: dict[str, Any], patch: dict[str, Any]) -> bool:
    if not patch:
        return False
    new_sessions = int(patch.get("sessions_available") or 0)
    old_sessions = int(existing.get("sessions_available") or 0)
    if new_sessions > old_sessions:
        return True
    if not existing and new_sessions > 0:
        return True
    for h in _DEFAULT_FORWARD_OUTCOME_HORIZONS:
        key = f"forward_{h}d_pct"
        new_v = patch.get(key)
        old_v = existing.get(key)
        if new_v is not None and old_v is None:
            return True
    dd_key = f"max_drawdown_{max(_DEFAULT_FORWARD_OUTCOME_HORIZONS)}d_pct"
    if patch.get(dd_key) is not None and existing.get(dd_key) is None:
        return True
    return False


def compute_forward_outcome_patches(
    rows: Sequence[dict[str, Any]],
    *,
    start_iso: str,
    end_iso: str,
    horizons: Sequence[int] = _DEFAULT_FORWARD_OUTCOME_HORIZONS,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return ``(sector, symbol, trade_date)`` -> sanitized ``forward_outcomes`` tape patch."""
    if not rows:
        return {}
    fre = _import_forward_return_eval()
    indexed = fre.compute_forward_outcomes_for_rows(
        rows,
        start=start_iso,
        end=end_iso,
        horizons=horizons,
    )
    sector_by_symbol_date: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        td = str(row.get("trade_date") or "").strip()[:10]
        sec = str(row.get("sector") or "").strip().lower()
        if sym and td and sec:
            sector_by_symbol_date[(sym, td)] = sec

    computed_at = datetime.now(IST).isoformat(timespec="seconds")
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (sym, td), obs in indexed.items():
        sec = sector_by_symbol_date.get((sym, td), "")
        if not sec:
            continue
        patch = fre.build_tape_forward_outcomes_patch(
            obs,
            horizons=horizons,
            computed_at=computed_at,
        )
        out[(sec, sym, td)] = sanitize_for_json(patch)
    return out


def _fetch_feature_rows_for_forward_outcomes(
    client: Any,
    *,
    sector: str | None,
    all_stocks: bool,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    select_cols = "trade_date,sector,symbol,exchange,action_signal,return_1d_pct,tape_extras"
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 1000
    while True:
        q = (
            client.table("symbol_daily_features")
            .select(select_cols)
            .gte("trade_date", start_iso)
            .lte("trade_date", end_iso)
            .order("trade_date")
        )
        if not all_stocks and sector:
            q = q.eq("sector", str(sector).strip().lower())
        q = q.range(offset, offset + page - 1)
        batch = list(getattr(q.execute(), "data", None) or [])
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def persist_forward_outcomes(
    cfg: TitanConfig,
    *,
    client: Any | None = None,
    sector: str | None = None,
    all_stocks: bool = False,
    as_of_date: date | str | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """Backfill/update ``tape_extras.forward_outcomes`` for matured signal dates (NaN-safe)."""
    if not forward_outcomes_persist_enabled():
        return {"enabled": False, "updated": 0}
    as_of = _parse_date(as_of_date) if isinstance(as_of_date, str) else as_of_date
    as_of = as_of or datetime.now(IST).date()
    lookback = lookback_days if lookback_days is not None else _forward_outcome_lookback_days()
    start = as_of - timedelta(days=max(7, int(lookback)))
    start_iso = start.isoformat()
    end_iso = as_of.isoformat()
    client = client or create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        rows = _fetch_feature_rows_for_forward_outcomes(
            client,
            sector=sector,
            all_stocks=all_stocks,
            start_iso=start_iso,
            end_iso=end_iso,
        )
        if not rows:
            return {
                "enabled": True,
                "updated": 0,
                "reason": "no_rows",
                "scope": "all-stocks" if all_stocks else str(sector or ""),
            }

        patches = compute_forward_outcome_patches(rows, start_iso=start_iso, end_iso=end_iso)
        row_index: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sec = str(row.get("sector") or "").strip().lower()
            sym = str(row.get("symbol") or "").strip().upper()
            td = str(row.get("trade_date") or "").strip()[:10]
            exch = str(row.get("exchange") or "NSE").strip().upper() or "NSE"
            if sec and sym and td:
                row_index[(sec, sym, td)] = row

        updates: list[dict[str, Any]] = []
        for key, patch in patches.items():
            row = row_index.get(key)
            if row is None:
                continue
            existing = _existing_forward_outcomes(_safe_tape_extras(row))
            if not _forward_outcomes_need_update(existing, patch):
                continue
            tape = dict(_safe_tape_extras(row))
            tape["forward_outcomes"] = patch
            updates.append(
                sanitize_for_json(
                    {
                        "trade_date": key[2],
                        "sector": key[0],
                        "symbol": key[1],
                        "exchange": str(row.get("exchange") or "NSE").strip().upper() or "NSE",
                        "tape_extras": tape,
                    }
                )
            )

        if not updates:
            return {
                "enabled": True,
                "updated": 0,
                "candidates": len(patches),
                "scope": "all-stocks" if all_stocks else str(sector or ""),
            }

        client.table("symbol_daily_features").upsert(
            updates,
            on_conflict="trade_date,sector,symbol,exchange",
        ).execute()
        return {
            "enabled": True,
            "updated": len(updates),
            "candidates": len(patches),
            "scope": "all-stocks" if all_stocks else str(sector or ""),
            "start": start_iso,
            "end": end_iso,
        }
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        logger.warning("Forward outcomes persist failed: %s", msg)
        return {"enabled": True, "updated": 0, "reason": "api_error", "message": msg}
    except Exception as e:  # pragma: no cover
        logger.warning("Forward outcomes persist failed: %s", e)
        return {"enabled": True, "updated": 0, "reason": "unexpected"}


def _forward_returns_after(symbol_rows: Sequence[dict[str, Any]], as_of_iso: str) -> tuple[float, float]:
    """Compounded forward returns over the +1 and +5 sessions AFTER ``as_of_iso``.

    Derived from the stored trailing ``return_1d_pct`` of the FOLLOWING sessions
    (no same-day move). Returns ``(nan, nan)`` when no forward sessions are stored,
    so callers stay backward-compatible in the normal as-of-latest reconcile path.
    """
    forward = sorted(
        (
            r
            for r in symbol_rows
            if isinstance(r, dict) and str(r.get("trade_date") or "")[:10] > as_of_iso
        ),
        key=lambda r: str(r.get("trade_date") or ""),
    )
    if not forward:
        return float("nan"), float("nan")
    fwd_1d = float("nan")
    fwd_5d = float("nan")
    cum = 1.0
    for i, r in enumerate(forward[:5]):
        v = _safe_float(r.get("return_1d_pct"))
        if math.isnan(v):
            continue
        cum *= 1.0 + v / 100.0
        if i == 0:
            fwd_1d = (cum - 1.0) * 100.0
    if any(not math.isnan(_safe_float(r.get("return_1d_pct"))) for r in forward[:5]):
        fwd_5d = (cum - 1.0) * 100.0
    return fwd_1d, fwd_5d


VALID_ACTION_SIGNALS: tuple[str, ...] = ("buy", "hold", "trim", "exit-risk")
_ACCUMULATE_SIGNAL = "accumulate"


def valid_action_signals() -> tuple[str, ...]:
    """Active action-signal vocabulary (includes ``accumulate``)."""
    return VALID_ACTION_SIGNALS + (_ACCUMULATE_SIGNAL,)
DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS = 30
ONE_WEEK_TRADING_DAYS = 5
ONE_MONTH_TRADING_DAYS = 20

# Optional DB columns; omitted on upsert when PostgREST reports they are missing.
_SYMBOL_FEATURE_OPTIONAL_COLUMNS = frozenset(
    {
        "volume_participation_ratio",
        "next_day_score",
        "next_week_score",
        "news_correlation",
        "news_sentiment_aggregate",
        "news_sentiment_score",
        "news_sentiment_trend",
        "news_count",
        # Future signal-engine v2 outputs (no writer yet); optional so upserts degrade
        # gracefully when these columns are absent from the DB schema.
        "signal_confidence",
        "signal_reason_trace",
        "signal_engine_version",
    }
)
_SECTOR_ROLLUP_OPTIONAL_COLUMNS = frozenset({"pct_volume_participation_gt_1"})
_TAPE_EXTRAS_SCORE_KEYS = (
    "next_day_score",
    "next_week_score",
    "return_5d_pct",
    "return_10d_pct",
    "return_20d_pct",
    "sell_signal",
    "news_correlation",
)


def _is_missing_column_api_error(message: str) -> bool:
    msg = str(message or "").lower()
    return "column" in msg and (
        "does not exist" in msg
        or "could not find" in msg
        or "schema cache" in msg
    )


def _prune_row_columns(row: dict[str, Any], *, drop: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in drop}


def _upsert_rows_with_optional_columns(
    client: Any,
    table: str,
    rows: Sequence[dict[str, Any]],
    *,
    on_conflict: str,
    optional_columns: frozenset[str],
) -> None:
    payload = list(rows)
    try:
        client.table(table).upsert(payload, on_conflict=on_conflict).execute()
        return
    except APIError as e:
        payload_msg = e.args[0] if e.args else {}
        msg = payload_msg.get("message", str(e)) if isinstance(payload_msg, dict) else str(e)
        if not _is_missing_column_api_error(msg):
            raise
        logger.warning(
            "Upsert to %s failed due to missing optional columns; retrying slim payload. Details: %s",
            table,
            msg,
        )
    slim = [_prune_row_columns(row, drop=optional_columns) for row in payload]
    client.table(table).upsert(slim, on_conflict=on_conflict).execute()


def _normalize_action_signal(raw_signal: Any) -> str:
    text_signal = str(raw_signal or "").strip().lower()
    if text_signal in ("exit", "exit_risk", "exitrisk", "exit-risk"):
        return "exit-risk"
    if text_signal in valid_action_signals():
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
            "accumulate_signal_consistency_ratio": 0.0,
        }
    return {
        "buy_signal_consistency_ratio": round(signal_sequence.count("buy") / total_points, 4),
        "hold_signal_consistency_ratio": round(signal_sequence.count("hold") / total_points, 4),
        "trim_signal_consistency_ratio": round(signal_sequence.count("trim") / total_points, 4),
        "exit_risk_signal_consistency_ratio": round(signal_sequence.count("exit-risk") / total_points, 4),
        "accumulate_signal_consistency_ratio": round(
            signal_sequence.count(_ACCUMULATE_SIGNAL) / total_points, 4
        ),
    }


def _evaluate_transition_horizon_outcome(
    target_signal: str,
    realized_return_pct: float | None,
) -> str | None:
    if realized_return_pct is None or math.isnan(realized_return_pct):
        return None
    outcome_threshold_pct = 0.25
    # ``accumulate`` is a constructive (bullish-leaning) label, grouped with buy/hold.
    is_bullish_signal = target_signal in ("buy", "accumulate", "hold")
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
        "ohlc_bar_as_of_date": audit.get("ohlc_bar_as_of_date"),
        "ohlc_bar_incomplete": audit.get("ohlc_bar_incomplete"),
        "session_move_vs_prev_close_pct": audit.get("session_move_vs_prev_close_pct"),
        "price_snapshot_ts": audit.get("price_snapshot_ts"),
        # v2 risk-gate inputs (Layers C/C-8/D): computed in build_equity_live_audit but
        # previously never persisted, so backtests/reconcile ran money-flow / ADX-regime /
        # over-extension layers at zero. Persist them so they are reproducible offline.
        "cmf_20": audit.get("cmf_20"),
        "obv_slope_20": audit.get("obv_slope_20"),
        "obv_latest": audit.get("obv_latest"),
        "obv_ema_20": audit.get("obv_ema_20"),
        "obv_trend_confirm": audit.get("obv_trend_confirm"),
        "adx_14": audit.get("adx_14"),
        "adx_plus_di_14": audit.get("adx_plus_di_14"),
        "adx_minus_di_14": audit.get("adx_minus_di_14"),
        "ema200_stretch_atr": audit.get("ema200_stretch_atr"),
        "sector_pctile_ema200_stretch": audit.get("sector_pctile_ema200_stretch"),
        "sector_pctile_cmf_20": audit.get("sector_pctile_cmf_20"),
        "sector_pctile_adx_14": audit.get("sector_pctile_adx_14"),
        "adx_regime_mults": (
            (audit.get("signal_reason_trace") or {}).get("adx_regime_mults")
            if isinstance(audit.get("signal_reason_trace"), dict)
            else None
        ),
        "sell_signal_risk_score": audit.get("sell_signal_risk_score"),
    }

    vpr = audit.get("volume_participation_ratio", audit.get("absorption_ratio"))
    next_day_score = audit.get("next_day_score")
    next_week_score = audit.get("next_week_score")

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
            "volume_participation_ratio": vpr,
            "absorption_ratio": audit.get("absorption_ratio", vpr),
            "next_day_score": next_day_score,
            "next_week_score": next_week_score,
            "return_1d_pct": audit.get("return_1d_pct"),
            "ema_200_distance_pct": audit.get("ema_200_distance_pct"),
            "atr_14_pct": audit.get("atr_14_pct"),
            "flags": flags,
            "option_chain_unavailable": bool(audit.get("option_chain_unavailable", False)),
            "rows_count": int(audit.get("rows") or 0),
            "tape_extras": tape_extras,
            "news_correlation": audit.get("news_correlation"),
            "news_sentiment_aggregate": audit.get("news_sentiment_aggregate"),
            "news_sentiment_score": audit.get("news_sentiment_score"),
            "news_sentiment_trend": audit.get("news_sentiment_trend"),
            "news_count": audit.get("news_count"),
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
        _upsert_rows_with_optional_columns(
            client,
            "symbol_daily_features",
            features,
            on_conflict="trade_date,sector,symbol,exchange",
            optional_columns=_SYMBOL_FEATURE_OPTIONAL_COLUMNS,
        )
        forward_outcomes_meta = persist_forward_outcomes(
            cfg,
            client=client,
            sector=sector,
            as_of_date=run_ts.date(),
        )
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
        _upsert_rows_with_optional_columns(
            client,
            "sector_daily_rollup",
            [rollup],
            on_conflict="trade_date,sector",
            optional_columns=_SECTOR_ROLLUP_OPTIONAL_COLUMNS,
        )
        return {
            "enabled": True,
            "persisted": True,
            "run_id": run_id,
            "feature_rows": len(features),
            "forward_outcomes": forward_outcomes_meta,
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
        forward_outcomes_meta = persist_forward_outcomes(
            cfg,
            client=client,
            sector=sector,
            as_of_date=as_of,
        )
        return {
            "enabled": True,
            "updated": True,
            "rows": len(to_upsert),
            "forward_outcomes": forward_outcomes_meta,
        }
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
    out = dict(x) if isinstance(x, dict) else {}
    for key in _TAPE_EXTRAS_SCORE_KEYS:
        if key not in out and row.get(key) is not None:
            out[key] = row.get(key)
    return out


def _reconcile_data_sparse_reason(
    *,
    symbol_count: int,
    coverage_next_day: int,
    coverage_next_week: int,
    features_row_count: int,
    symbols_with_prior_prediction: int,
) -> str | None:
    if symbol_count <= 0 and features_row_count <= 0:
        return "no_symbol_daily_features_in_lookback"
    if coverage_next_day <= 0 and coverage_next_week <= 0:
        if symbols_with_prior_prediction <= 0:
            return "need_at_least_two_trading_days_with_predictions"
        return "predictions_not_matured_against_realized_returns"
    return None


def _safe_json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_news_direction(news_correlation: Any) -> str:
    corr = _safe_json_dict(news_correlation)
    direction = str(corr.get("direction") or "").strip().lower()
    if direction in ("tailwind", "positive", "up", "bullish"):
        return "up"
    if direction in ("headwind", "negative", "down", "bearish"):
        return "down"
    return "neutral"


def _symbol_row_key(*, sector: str, symbol: str, exchange: str) -> str:
    sec = str(sector or "unknown").strip().lower() or "unknown"
    sym = str(symbol or "").strip().upper()
    exch = str(exchange or "NSE").strip().upper() or "NSE"
    return f"{sec}|{sym}|{exch}"


def _display_symbol(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "").strip().upper()
    sector = str(row.get("sector") or "").strip().lower()
    if not symbol:
        return "unknown"
    if sector:
        return f"{symbol}({sector})"
    return symbol


def _rate_pct(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round((100.0 * num) / den, 1)


def _classify_failure_reason(*, row: dict[str, Any], transition_row: dict[str, Any]) -> str:
    if bool(transition_row.get("is_whipsaw_transition")):
        return "whipsaw"
    news_direction = str(row.get("news_direction") or "neutral").strip().lower()
    if news_direction == "down":
        return "news_shock"
    stability = _safe_float(transition_row.get("transition_stability_score"))
    if not math.isnan(stability) and stability < 40.0:
        return "volatility_regime_mismatch"
    return "technical_overfit"


def _fetch_reconcile_table_inputs(
    cfg: TitanConfig,
    *,
    sector: str | None = None,
    all_stocks: bool = False,
    as_of_trade_date: str | None = None,
    lookback_days: int = 120,
) -> dict[str, Any]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    end_date = _parse_date(as_of_trade_date) or datetime.now(IST).date()
    start_date = end_date - timedelta(days=max(20, int(lookback_days)))
    out: dict[str, Any] = {
        "sector": (str(sector or "").strip().lower() if not all_stocks else "all-stocks"),
        "scope": "all-stocks" if all_stocks else "sector",
        "as_of_trade_date": end_date.isoformat(),
        "symbol_daily_features": [],
        "stock_signal_transition_analytics": [],
        "sector_daily_winners": [],
        "sector_daily_rollup": [],
        "global_news_snapshots": [],
        "llm_digest_memory": [],
    }
    feature_select_candidates = [
        "trade_date,symbol,exchange,action_signal,return_1d_pct,tape_extras,next_day_score,next_week_score",
        "trade_date,symbol,exchange,action_signal,return_1d_pct,tape_extras",
        "trade_date,symbol,exchange,action_signal,return_1d_pct",
    ]
    for feature_select in feature_select_candidates:
        try:
            features_res = (
                client.table("symbol_daily_features")
                .select(feature_select)
                .gte("trade_date", start_date.isoformat())
                .lte("trade_date", end_date.isoformat())
                .order("trade_date", desc=True)
            )
            if not all_stocks:
                features_res = features_res.eq("sector", str(sector or "").strip().lower())
            features_res = features_res.execute()
            out["symbol_daily_features"] = list(getattr(features_res, "data", None) or [])
            break
        except Exception as e:  # pragma: no cover
            logger.warning(
                "Reconcile fetch failed for symbol_daily_features %s (%s): %s",
                sector,
                feature_select,
                e,
            )
    try:
        transition_res = (
            client.table("stock_signal_transition_analytics")
            .select(
                "trade_date,symbol,exchange,previous_signal,current_signal,transition_type,"
                "transition_stability_score,is_whipsaw_transition,whipsaw_transition_count,"
                "matured_1w_available,matured_1w_outcome,matured_1w_realized_return_pct,"
                "matured_1m_available,matured_1m_outcome,matured_1m_realized_return_pct"
            )
            .eq("trailing_window_days", DEFAULT_TRANSITION_TRAILING_WINDOW_DAYS)
            .gte("trade_date", start_date.isoformat())
            .lte("trade_date", end_date.isoformat())
            .order("trade_date", desc=True)
        )
        if not all_stocks:
            transition_res = transition_res.eq("sector", str(sector or "").strip().lower())
        transition_res = transition_res.execute()
        out["stock_signal_transition_analytics"] = list(getattr(transition_res, "data", None) or [])
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile fetch failed for stock_signal_transition_analytics %s: %s", sector, e)
    try:
        winners_res = (
            client.table("sector_daily_winners")
            .select("as_of_date,symbol,exchange,winner_rank,rank_score,score_breakdown,source_meta")
            .gte("as_of_date", start_date.isoformat())
            .lte("as_of_date", end_date.isoformat())
            .order("as_of_date", desc=True)
            .order("winner_rank")
        )
        if not all_stocks:
            winners_res = winners_res.eq("sector_key", str(sector or "").strip().lower())
        winners_res = winners_res.execute()
        out["sector_daily_winners"] = list(getattr(winners_res, "data", None) or [])
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile fetch failed for sector_daily_winners %s: %s", sector, e)
    try:
        rollup_res = (
            client.table("sector_daily_rollup")
            .select("trade_date,sector,symbol_count,avg_effective_intent_score,breadth_above_ema200_pct,pct_absorption_gt_1")
            .gte("trade_date", start_date.isoformat())
            .lte("trade_date", end_date.isoformat())
            .order("trade_date", desc=True)
        )
        if not all_stocks:
            rollup_res = rollup_res.eq("sector", str(sector or "").strip().lower())
        rollup_res = rollup_res.execute()
        out["sector_daily_rollup"] = list(getattr(rollup_res, "data", None) or [])
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile fetch failed for sector_daily_rollup %s: %s", sector, e)
    try:
        news_res = (
            client.table("global_news_snapshots")
            .select("refreshed_at,item_count,fetch_status,news_items,sector_scores")
            .order("refreshed_at", desc=True)
            .limit(1)
            .execute()
        )
        out["global_news_snapshots"] = list(getattr(news_res, "data", None) or [])
    except Exception as e:  # pragma: no cover
        logger.warning("Reconcile fetch failed for global_news_snapshots: %s", e)
    # Optional narrative context only; do not make reconcile dependent on this table.
    try:
        memory_res = (
            client.table("llm_digest_memory")
            .select("run_id,recorded_at,output_text,full_digest")
            .order("recorded_at", desc=True)
            .limit(1)
        )
        if not all_stocks:
            memory_res = memory_res.eq("sector", str(sector or "").strip().lower())
        memory_res = memory_res.execute()
        out["llm_digest_memory"] = list(getattr(memory_res, "data", None) or [])
    except Exception:  # pragma: no cover
        out["llm_digest_memory"] = []
    return out


def build_stock_reconcile_snapshot(
    audits: Sequence[dict[str, Any]],
    *,
    historical_rows_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    table_inputs: dict[str, Any] | None = None,
    as_of_trade_date: str | None = None,
) -> dict[str, Any]:
    if table_inputs:
        as_of = _parse_date(as_of_trade_date or table_inputs.get("as_of_trade_date")) or datetime.now(IST).date()
        as_of_iso = as_of.isoformat()
        features_all = [
            row
            for row in _safe_json_list(table_inputs.get("symbol_daily_features"))
            if isinstance(row, dict)
        ]
        features_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in features_all:
            td = _parse_date(row.get("trade_date"))
            if td is None or td > as_of:
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            features_by_symbol[symbol].append(row)
        for symbol in list(features_by_symbol.keys()):
            features_by_symbol[symbol].sort(
                key=lambda row: _parse_date(row.get("trade_date")) or date.min,
                reverse=True,
            )

        transitions_for_day = [
            row
            for row in _safe_json_list(table_inputs.get("stock_signal_transition_analytics"))
            if isinstance(row, dict) and str(row.get("trade_date") or "") == as_of_iso
        ]
        transition_by_symbol: dict[str, dict[str, Any]] = {}
        for row in transitions_for_day:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                transition_by_symbol[symbol] = row

        winners_for_day = [
            row
            for row in _safe_json_list(table_inputs.get("sector_daily_winners"))
            if isinstance(row, dict) and str(row.get("as_of_date") or "") == as_of_iso
        ]
        winner_symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in winners_for_day
            if str(row.get("symbol") or "").strip()
        }

        rollup_today = next(
            (
                row
                for row in _safe_json_list(table_inputs.get("sector_daily_rollup"))
                if isinstance(row, dict) and str(row.get("trade_date") or "") == as_of_iso
            ),
            {},
        )
        news_snapshot = next(
            (
                row
                for row in _safe_json_list(table_inputs.get("global_news_snapshots"))
                if isinstance(row, dict)
            ),
            {},
        )

        accuracy: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: {"next_day": {"hit": 0, "total": 0}, "next_week": {"hit": 0, "total": 0}}
        )
        success_symbols: list[str] = []
        failure_symbols: list[str] = []
        failure_reason_counts: dict[str, int] = defaultdict(int)
        transition_quality: dict[str, int] = defaultdict(int)
        transition_outcome_1w: dict[str, int] = defaultdict(int)
        transition_outcome_1m: dict[str, int] = defaultdict(int)
        per_symbol: dict[str, dict[str, Any]] = {}
        news_rows: list[dict[str, Any]] = []

        whipsaw_total = 0
        whipsaw_symbols = 0
        stability_scores: list[float] = []
        symbols_with_prior_prediction = 0
        shortlist_total = 0
        shortlist_1d_hit = 0
        shortlist_5d_hit = 0
        shortlist_5d_cov = 0
        news_total = 0
        news_aligned = 0

        symbols_to_process = set(features_by_symbol.keys()) | {
            str(audit.get("symbol") or "").strip().upper()
            for audit in audits
            if str(audit.get("symbol") or "").strip()
        }
        for symbol in sorted(symbols_to_process):
            symbol_rows = features_by_symbol.get(symbol, [])
            current = symbol_rows[0] if symbol_rows else {}
            if str((current or {}).get("trade_date") or "") != as_of_iso and symbol_rows:
                current = next(
                    (row for row in symbol_rows if str(row.get("trade_date") or "") == as_of_iso),
                    {},
                )
            previous = symbol_rows[1] if len(symbol_rows) >= 2 else {}
            week_ref = symbol_rows[5] if len(symbol_rows) >= 6 else {}
            current_tape = _safe_tape_extras(current)
            previous_tape = _safe_tape_extras(previous)
            week_tape = _safe_tape_extras(week_ref)

            signal = _normalize_action_signal(
                current.get("action_signal")
                or current_tape.get("sell_signal")
                or previous.get("action_signal")
                or "hold"
            )
            pred_1d = _score_direction(previous_tape.get("next_day_score"))
            if pred_1d != "unknown":
                symbols_with_prior_prediction += 1
            realized_1d = _return_direction(current.get("return_1d_pct"))
            hit_1d = _direction_hit(pred_1d, realized_1d)

            pred_1w = _score_direction(week_tape.get("next_week_score"))
            realized_1w = _return_direction(current_tape.get("return_5d_pct"))
            hit_1w = _direction_hit(pred_1w, realized_1w)
            if hit_1d is not None:
                accuracy[signal]["next_day"]["total"] += 1
                accuracy[signal]["next_day"]["hit"] += int(hit_1d)
            if hit_1w is not None:
                accuracy[signal]["next_week"]["total"] += 1
                accuracy[signal]["next_week"]["hit"] += int(hit_1w)

            tr = transition_by_symbol.get(symbol, {})
            prev_signal = _normalize_action_signal(tr.get("previous_signal") or previous.get("action_signal"))
            current_signal = _normalize_action_signal(tr.get("current_signal") or signal)
            is_actual_transition = prev_signal != current_signal
            transition_label = str(tr.get("transition_type") or f"{prev_signal}_to_{signal}")
            tr_quality = str(tr.get("matured_1w_outcome") or "unknown").lower()
            transition_quality[tr_quality] += 1
            transition_outcome_1w[str(tr.get("matured_1w_outcome") or "unknown").lower()] += 1
            transition_outcome_1m[str(tr.get("matured_1m_outcome") or "unknown").lower()] += 1

            whipsaw_total += int(tr.get("whipsaw_transition_count") or 0)
            if bool(tr.get("is_whipsaw_transition")):
                whipsaw_symbols += 1
            stability = _safe_float(tr.get("transition_stability_score"))
            if not math.isnan(stability):
                stability_scores.append(stability)

            is_shortlist = symbol in winner_symbols
            if is_shortlist:
                shortlist_total += 1
                ret_1d = _safe_float(current.get("return_1d_pct"))
                ret_5d = _safe_float(current_tape.get("return_5d_pct"))
                if forward_return_eval_enabled():
                    fwd_1d, fwd_5d = _forward_returns_after(symbol_rows, as_of_iso)
                    if not math.isnan(fwd_1d):
                        ret_1d = fwd_1d
                    if not math.isnan(fwd_5d):
                        ret_5d = fwd_5d
                if not math.isnan(ret_1d) and ret_1d > 0:
                    shortlist_1d_hit += 1
                if not math.isnan(ret_5d):
                    shortlist_5d_cov += 1
                    if ret_5d > 0:
                        shortlist_5d_hit += 1

            news_correlation = current_tape.get("news_correlation")
            news_dir = _parse_news_direction(news_correlation)
            if news_dir != "neutral":
                news_total += 1
                if news_dir == realized_1d:
                    news_aligned += 1
            news_summary = _news_summary_from_audit({"news_correlation": news_correlation})
            symbol_label = _display_symbol(
                {
                    "symbol": symbol,
                    "sector": current.get("sector") or previous.get("sector"),
                }
            )
            matured_1w_available = bool(tr.get("matured_1w_available"))
            matured_1m_available = bool(tr.get("matured_1m_available"))
            transition_evaluable = (
                matured_1w_available
                or matured_1m_available
                or (hit_1d is not None)
                or (hit_1w is not None)
            )
            transition_beneficial: bool | None
            if transition_evaluable:
                transition_beneficial = (
                    tr_quality == "favorable"
                    or (hit_1d is True and hit_1w in (True, None))
                )
            else:
                transition_beneficial = None
            transition_outcome_label = (
                "beneficial"
                if transition_beneficial is True
                else ("not-beneficial" if transition_beneficial is False else "not_evaluable")
            )
            symbol_success = (hit_1d is True) or (hit_1w is True)
            symbol_failure = (hit_1d is False) or (hit_1w is False)
            failure_reason = (
                _classify_failure_reason(row={"news_direction": news_dir}, transition_row=tr)
                if symbol_failure
                else ""
            )
            if symbol_success:
                success_symbols.append(symbol_label)
            if symbol_failure:
                failure_symbols.append(symbol_label)
                if failure_reason:
                    failure_reason_counts[failure_reason] += 1

            row = {
                "symbol": symbol,
                "sector": str(current.get("sector") or previous.get("sector") or "").strip().lower(),
                "signal": signal,
                "pred_next_day": pred_1d,
                "realized_next_day": realized_1d,
                "hit_next_day": hit_1d,
                "pred_next_week": pred_1w,
                "realized_next_week": realized_1w,
                "hit_next_week": hit_1w,
                "signal_transition": transition_label.replace("_to_", "->"),
                "previous_signal": prev_signal,
                "current_signal": current_signal,
                "is_actual_transition": bool(is_actual_transition),
                "transition_evaluable": bool(transition_evaluable),
                "transition_quality": tr_quality,
                "transition_beneficial": transition_beneficial,
                "transition_outcome_label": transition_outcome_label,
                "news_summary": news_summary,
                "news_direction": news_dir,
                "failure_reason": failure_reason,
                "is_success": bool(symbol_success),
                "is_failure": bool(symbol_failure),
                "shortlist_member": is_shortlist,
            }
            per_symbol[symbol] = row
            if news_summary != "news: n/a":
                news_rows.append({"symbol": symbol, "signal": signal, "summary": news_summary})

        coverage_1d = sum(v["next_day"]["total"] for v in accuracy.values())
        coverage_1w = sum(v["next_week"]["total"] for v in accuracy.values())
        data_sparse_reason = _reconcile_data_sparse_reason(
            symbol_count=len(per_symbol),
            coverage_next_day=coverage_1d,
            coverage_next_week=coverage_1w,
            features_row_count=len(features_all),
            symbols_with_prior_prediction=symbols_with_prior_prediction,
        )
        whipsaw_rate = (100.0 * whipsaw_symbols / len(per_symbol)) if per_symbol else None
        avg_stability = round(sum(stability_scores) / len(stability_scores), 2) if stability_scores else None
        shortlist_1d_rate = (100.0 * shortlist_1d_hit / shortlist_total) if shortlist_total else None
        shortlist_5d_rate = (100.0 * shortlist_5d_hit / shortlist_5d_cov) if shortlist_5d_cov else None
        news_alignment_rate = (100.0 * news_aligned / news_total) if news_total else None
        narrative_context = next(
            (
                str(row.get("output_text") or row.get("full_digest") or "").strip()
                for row in _safe_json_list(table_inputs.get("llm_digest_memory"))
                if isinstance(row, dict)
            ),
            "",
        )
        return sanitize_for_json(
            {
                "as_of_trade_date": as_of_iso,
                "symbol_count": len(per_symbol),
                "scope": str(table_inputs.get("scope") or "sector"),
                "coverage_next_day": coverage_1d,
                "coverage_next_week": coverage_1w,
                "data_sparse_reason": data_sparse_reason,
                "symbols_with_prior_prediction": symbols_with_prior_prediction,
                "success_count": len(success_symbols),
                "failure_count": len(failure_symbols),
                "success_symbols": success_symbols[:30],
                "failure_symbols": failure_symbols[:30],
                "failure_reason_counts": {k: int(v) for k, v in failure_reason_counts.items()},
                "accuracy_by_signal_horizon": {k: v for k, v in accuracy.items()},
                "transition_quality": {k: int(v) for k, v in transition_quality.items()},
                "transition_outcome_1w": {k: int(v) for k, v in transition_outcome_1w.items()},
                "transition_outcome_1m": {k: int(v) for k, v in transition_outcome_1m.items()},
                "whipsaw_transition_total": int(whipsaw_total),
                "whipsaw_symbol_rate_pct": round(whipsaw_rate, 2) if whipsaw_rate is not None else None,
                "avg_transition_stability_score": avg_stability,
                "shortlist_efficacy": {
                    "count": shortlist_total,
                    "hit_1d": shortlist_1d_hit,
                    "hit_rate_1d_pct": (round(shortlist_1d_rate, 2) if shortlist_1d_rate is not None else None),
                    "coverage_5d": shortlist_5d_cov,
                    "hit_5d": shortlist_5d_hit,
                    "hit_rate_5d_pct": (round(shortlist_5d_rate, 2) if shortlist_5d_rate is not None else None),
                },
                "news_attribution_efficacy": {
                    "evaluated_symbols": news_total,
                    "aligned_symbols": news_aligned,
                    "alignment_rate_pct": (
                        round(news_alignment_rate, 2) if news_alignment_rate is not None else None
                    ),
                    "snapshot_status": str(news_snapshot.get("fetch_status") or "n/a"),
                    "snapshot_refreshed_at": str(news_snapshot.get("refreshed_at") or ""),
                    "sector_score": _safe_json_dict(news_snapshot.get("sector_scores")).get(
                        str(table_inputs.get("sector") or "")
                    ),
                },
                "regime_context": rollup_today if isinstance(rollup_today, dict) else {},
                "news_attribution_summaries": news_rows[:20],
                "narrative_context": narrative_context[:500],
                "per_symbol": per_symbol,
            }
        )

    historical_rows_by_symbol = historical_rows_by_symbol or {}
    accuracy: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {"next_day": {"hit": 0, "total": 0}, "next_week": {"hit": 0, "total": 0}}
    )
    success_symbols: list[str] = []
    failure_symbols: list[str] = []
    failure_reason_counts: dict[str, int] = defaultdict(int)
    transition_counts: dict[str, int] = defaultdict(int)
    per_symbol: dict[str, dict[str, Any]] = {}
    news_rows: list[dict[str, Any]] = []
    symbols_with_prior_prediction = 0

    for audit in audits:
        symbol = str(audit.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        signal = _normalize_action_signal(audit.get("action_signal", audit.get("sell_signal")))
        hist = historical_rows_by_symbol.get(symbol, [])
        prev = hist[0] if hist else {}
        prev_tape = _safe_tape_extras(prev) if isinstance(prev, dict) else {}
        if _score_direction(prev_tape.get("next_day_score")) != "unknown":
            symbols_with_prior_prediction += 1
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
        is_actual_transition = prev_signal != signal
        news_summary = _news_summary_from_audit(audit)
        news_dir = _parse_news_direction(audit.get("news_correlation"))
        transition_evaluable = (hit_1d is not None) or (hit_1w is not None)
        transition_beneficial: bool | None
        if transition_evaluable:
            transition_beneficial = q in ("improving", "favorable")
        else:
            transition_beneficial = None
        transition_outcome_label = (
            "beneficial"
            if transition_beneficial is True
            else ("not-beneficial" if transition_beneficial is False else "not_evaluable")
        )
        symbol_success = (hit_1d is True) or (hit_1w is True)
        symbol_failure = (hit_1d is False) or (hit_1w is False)
        failure_reason = (
            _classify_failure_reason(
                row={"news_direction": news_dir},
                transition_row={"is_whipsaw_transition": q == "deteriorating"},
            )
            if symbol_failure
            else ""
        )
        if symbol_success:
            success_symbols.append(symbol)
        if symbol_failure:
            failure_symbols.append(symbol)
            if failure_reason:
                failure_reason_counts[failure_reason] += 1

        row = {
            "symbol": symbol,
            "sector": str(audit.get("sector") or "").strip().lower(),
            "signal": signal,
            "pred_next_day": pred_1d,
            "realized_next_day": realized_1d,
            "hit_next_day": hit_1d,
            "pred_next_week": pred_1w,
            "realized_next_week": realized_1w,
            "hit_next_week": hit_1w,
            "signal_transition": signal_transition,
            "previous_signal": prev_signal,
            "current_signal": signal,
            "is_actual_transition": bool(is_actual_transition),
            "transition_evaluable": bool(transition_evaluable),
            "transition_quality": q,
            "transition_beneficial": transition_beneficial,
            "transition_outcome_label": transition_outcome_label,
            "news_summary": news_summary,
            "news_direction": news_dir,
            "failure_reason": failure_reason,
            "is_success": bool(symbol_success),
            "is_failure": bool(symbol_failure),
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
            "scope": "sector",
            "coverage_next_day": coverage_1d,
            "coverage_next_week": coverage_1w,
            "data_sparse_reason": _reconcile_data_sparse_reason(
                symbol_count=len(per_symbol),
                coverage_next_day=coverage_1d,
                coverage_next_week=coverage_1w,
                features_row_count=sum(len(v) for v in historical_rows_by_symbol.values()),
                symbols_with_prior_prediction=symbols_with_prior_prediction,
            ),
            "symbols_with_prior_prediction": symbols_with_prior_prediction,
            "success_count": len(success_symbols),
            "failure_count": len(failure_symbols),
            "success_symbols": success_symbols[:30],
            "failure_symbols": failure_symbols[:30],
            "failure_reason_counts": {k: int(v) for k, v in failure_reason_counts.items()},
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
    def _symbols_text(symbols: list[str], *, max_symbols: int = 10) -> str:
        if not symbols:
            return "none"
        uniq = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not uniq:
            return "none"
        if len(uniq) <= max_symbols:
            return ", ".join(uniq)
        shown = ", ".join(uniq[:max_symbols])
        return f"{shown} (+{len(uniq) - max_symbols} more)"

    def _avg_return(rows: list[dict[str, Any]], key: str) -> float | None:
        vals = [_safe_float(r.get(key)) for r in rows]
        vals = [x for x in vals if not math.isnan(x)]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    def _pct(n: int, d: int) -> str:
        p = _rate_pct(n, d)
        return "n/a" if p is None else f"{p:.1f}%"

    lines = ["--- EOD Reconcile (Decision-first) ---"]
    as_of = str(summary.get("as_of_trade_date") or "").strip()
    if as_of:
        lines.append(f"As-of trade date: {as_of}")
    scope = str(summary.get("scope") or "sector")
    symbol_count = int(summary.get("symbol_count") or 0)
    coverage_next_day = int(summary.get("coverage_next_day") or 0)
    coverage_next_week = int(summary.get("coverage_next_week") or 0)
    no_matured_coverage = coverage_next_day <= 0 and coverage_next_week <= 0
    news_eff = summary.get("news_attribution_efficacy")
    news_eff = news_eff if isinstance(news_eff, dict) else {}
    confidence = news_eff.get("alignment_rate_pct")
    confidence_txt = "n/a" if confidence is None else f"{confidence}%"
    cov_1d_txt = str(coverage_next_day) if coverage_next_day > 0 else "insufficient matured data"
    cov_1w_txt = str(coverage_next_week) if coverage_next_week > 0 else "insufficient matured data"
    sparse_reason = str(summary.get("data_sparse_reason") or "").strip()
    sparse_hints = {
        "no_symbol_daily_features_in_lookback": (
            "No symbol_daily_features rows in lookback; run Titan with TITAN_ENABLE_ANALYSIS_STORE=1."
        ),
        "need_at_least_two_trading_days_with_predictions": (
            "Need at least two trading days with next_day_score in tape_extras (or top-level columns) per symbol."
        ),
        "predictions_not_matured_against_realized_returns": (
            "Prior-day predictions exist but realized returns are missing for the as-of date."
        ),
    }
    lines.extend(
        [
            "",
            "A) Coverage and confidence",
            f"Scope: {scope} | universe symbols: {symbol_count}",
            f"Evaluated: next-day {cov_1d_txt} | next-week {cov_1w_txt} | news-correlation confidence: {confidence_txt}",
        ]
    )
    if no_matured_coverage and sparse_reason in sparse_hints:
        lines.append(f"Data note: {sparse_hints[sparse_reason]}")

    per_symbol = summary.get("per_symbol")
    per_symbol = per_symbol if isinstance(per_symbol, dict) else {}
    rows = [r for r in per_symbol.values() if isinstance(r, dict)]
    success_rows = [r for r in rows if bool(r.get("is_success"))]
    failure_rows = [r for r in rows if bool(r.get("is_failure"))]

    lines.extend(
        [
            "",
            "B) Success summary",
            (
                f"Count: {len(success_rows)} ({_pct(len(success_rows), len(rows))}) | avg 1D return: {_avg_return(success_rows, 'realized_next_day')} | avg 1W return: {_avg_return(success_rows, 'realized_next_week')}"
                if not no_matured_coverage
                else f"Count: {len(success_rows)} (insufficient matured data) | avg 1D return: n/a | avg 1W return: n/a"
            ),
            (
                "Top symbols: " + _symbols_text([_display_symbol(r) for r in success_rows])
                if not no_matured_coverage
                else f"Top symbols: none (0 evaluable symbols out of {len(rows)})"
            ),
        ]
    )

    reason_counts: dict[str, int] = defaultdict(int)
    news_headwind_symbols: list[str] = []
    for r in failure_rows:
        reason = str(r.get("failure_reason") or "technical_overfit").strip().lower()
        reason_counts[reason] += 1
        if str(r.get("news_direction") or "neutral").strip().lower() == "down":
            news_headwind_symbols.append(_display_symbol(r))
    lines.extend(
        [
            "",
            "C) Failure summary",
            (
                f"Count: {len(failure_rows)} ({_pct(len(failure_rows), len(rows))}) | avg 1D return: {_avg_return(failure_rows, 'realized_next_day')} | avg 1W return: {_avg_return(failure_rows, 'realized_next_week')}"
                if not no_matured_coverage
                else f"Count: {len(failure_rows)} (insufficient matured data) | avg 1D return: n/a | avg 1W return: n/a"
            ),
            (
                "Top symbols: " + _symbols_text([_display_symbol(r) for r in failure_rows])
                if not no_matured_coverage
                else f"Top symbols: none (0 evaluable symbols out of {len(rows)})"
            ),
            (
                "Failure reasons: "
                + (
                    ", ".join(
                        f"{k}={v}" for k, v in sorted(reason_counts.items(), key=lambda x: (-x[1], x[0]))
                    )
                    if reason_counts
                    else "none"
                )
                if not no_matured_coverage
                else "Failure reasons: insufficient matured data"
            ),
            (
                "News headwind evidence: " + _symbols_text(news_headwind_symbols)
                if not no_matured_coverage
                else "News headwind evidence: insufficient matured data"
            ),
        ]
    )

    transition_matrix: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"beneficial": 0, "not_beneficial": 0, "not_evaluable": 0, "symbols": []}
    )
    for r in rows:
        previous_signal = _normalize_action_signal(r.get("previous_signal"))
        current_signal = _normalize_action_signal(r.get("current_signal") or r.get("signal"))
        if previous_signal == current_signal:
            continue
        label = str(r.get("signal_transition") or "unknown->unknown").strip().upper()
        outcome_label = str(r.get("transition_outcome_label") or "").strip().lower().replace("-", "_")
        row = transition_matrix[label]
        if outcome_label == "beneficial":
            row["beneficial"] += 1
        elif outcome_label == "not_beneficial":
            row["not_beneficial"] += 1
        else:
            row["not_evaluable"] += 1
        row["symbols"].append(_display_symbol(r))

    lines.extend(["", "D) Signal transition matrix"])
    if transition_matrix:
        for tr_label, tr_row in sorted(
            transition_matrix.items(),
            key=lambda kv: -(
                int(kv[1]["beneficial"])
                + int(kv[1]["not_beneficial"])
                + int(kv[1]["not_evaluable"])
            ),
        )[:8]:
            total_evaluable = int(tr_row["beneficial"]) + int(tr_row["not_beneficial"])
            not_evaluable = int(tr_row["not_evaluable"])
            if total_evaluable <= 0:
                lines.append(
                    f"{tr_label}: insufficient matured data | not-evaluable {not_evaluable} | symbols: {_symbols_text(tr_row['symbols'], max_symbols=5)}"
                )
            else:
                lines.append(
                    f"{tr_label}: beneficial {tr_row['beneficial']} ({_pct(int(tr_row['beneficial']), total_evaluable)}) | not-beneficial {tr_row['not_beneficial']} ({_pct(int(tr_row['not_beneficial']), total_evaluable)}) | not-evaluable {not_evaluable} | symbols: {_symbols_text(tr_row['symbols'])}"
                )
    else:
        lines.append("No transition rows available.")

    lines.extend(["", "E) Key transition examples"])
    evaluable_rows = [
        r
        for r in rows
        if _normalize_action_signal(r.get("previous_signal")) != _normalize_action_signal(r.get("current_signal") or r.get("signal"))
        and str(r.get("transition_outcome_label") or "").strip().lower().replace("-", "_") != "not_evaluable"
    ]
    example_rows = sorted(
        evaluable_rows,
        key=lambda r: (
            0 if bool(r.get("transition_beneficial")) else 1,
            str(r.get("failure_reason") or ""),
        ),
    )[:5]
    if example_rows:
        for r in example_rows:
            lines.append(
                f"{_display_symbol(r)} {str(r.get('signal_transition') or 'unknown->unknown').upper()} | {str(r.get('transition_outcome_label') or 'n/a')} | why: {str(r.get('failure_reason') or 'model_aligned')} | {str(r.get('news_summary') or 'news: n/a')}"
            )
    else:
        lines.append("No evaluable transition examples (insufficient matured data).")

    flags: list[str] = []
    total_failures = len(failure_rows)
    if total_failures > 0:
        for reason_name in ("technical_overfit", "news_shock", "volatility_regime_mismatch", "whipsaw"):
            cnt = int(reason_counts.get(reason_name) or 0)
            pct = _rate_pct(cnt, total_failures)
            if pct is not None and pct >= 20.0:
                flags.append(f"{reason_name} elevated ({cnt}/{total_failures})")
    if news_headwind_symbols:
        flags.append(f"news headwind linked failures ({len(news_headwind_symbols)})")
    lines.extend(["", "F) Actionable model-improvement flags", "; ".join(flags) if flags else "No dominant risk flag."])
    return lines


def enrich_audits_with_stock_reconcile(
    cfg: TitanConfig,
    *,
    sector: str | None,
    all_stocks: bool = False,
    audits: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    table_inputs = _fetch_reconcile_table_inputs(
        cfg,
        sector=sector,
        all_stocks=all_stocks,
        lookback_days=120,
    )
    summary = build_stock_reconcile_snapshot(
        audits,
        historical_rows_by_symbol=None,
        table_inputs=table_inputs,
        as_of_trade_date=table_inputs.get("as_of_trade_date"),
    )
    # Backward-compatible fallback for environments without the new structured tables.
    if int(summary.get("symbol_count") or 0) <= 0 and not all_stocks and sector:
        history = _fetch_symbol_history_by_sector(cfg, sector=str(sector))
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
    sector: str | None,
    all_stocks: bool = False,
    days: int,
) -> dict[str, Any]:
    if days <= 0:
        return {"persisted": 0, "days": 0}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    table_inputs = _fetch_reconcile_table_inputs(
        cfg,
        sector=sector,
        all_stocks=all_stocks,
        lookback_days=max(120, int(days) + 15),
    )
    features = [
        row
        for row in _safe_json_list(table_inputs.get("symbol_daily_features"))
        if isinstance(row, dict) and str(row.get("trade_date") or "").strip()
    ]
    trade_dates = sorted({str(row.get("trade_date")) for row in features}, reverse=True)[:days]
    if not trade_dates:
        return {"persisted": 0, "days": 0, "reason": "no_trade_dates"}
    persisted = 0
    samples: list[dict[str, Any]] = []
    for td in trade_dates:
        summary = build_stock_reconcile_snapshot(
            [],
            historical_rows_by_symbol=None,
            table_inputs=table_inputs,
            as_of_trade_date=td,
        )
        lines = build_reconcile_digest_lines(summary)
        scope_label = "all-stocks" if all_stocks else str(sector or "unknown")
        run_id = f"reconcile-backfill-{scope_label}-{td}"
        payload = sanitize_for_json(
            {
                "run_id": run_id,
                "sector": (str(sector or "all-stocks")),
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
            if len(samples) < 2:
                samples.append(
                    {
                        "trade_date": td,
                        "line_count": len(lines),
                        "digest_preview": "\n".join(lines[:20]),
                    }
                )
        except Exception as e:  # pragma: no cover
            logger.warning("Reconcile backfill upsert failed for %s/%s: %s", scope_label, td, e)
    return {"persisted": persisted, "days": len(trade_dates), "samples": samples}


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
