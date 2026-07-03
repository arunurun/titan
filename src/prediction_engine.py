"""Predictive horizon scores — extracted from sector_audit._predictive_scores."""

from __future__ import annotations

import math
import os
from typing import Any

_CONTEMP_MOVE_THRESHOLD_PCT = 3.0
_CONTEMP_DISCOUNT_SLOPE = 0.08
_CONTEMP_MAX_DISCOUNT_FRAC = 0.5

PREDICTIVE_WEIGHT_DEFAULTS: dict[str, float] = {
    "tech_day": 0.52,
    "tech_week": 0.62,
    "ret1d": 0.42,
    "ret5d": 0.28,
    "ret10d": 0.15,
    "rel5": 0.20,
    "rel20": 0.11,
    "ema": 0.26,
    "atr": 0.45,
}


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp_score(x: float) -> float:
    return max(0.0, min(100.0, round(x, 2)))


def _ema_history_confidence(rows: Any) -> float:
    try:
        n = int(rows)
    except (TypeError, ValueError):
        return 1.0
    if n < 30:
        return 0.35
    return min(1.0, max(0.35, n / 200.0))


def _contemp_env_float(name: str, default: float) -> float:
    raw = (str(os.environ.get(name, "")) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _contemporaneous_dampener_enabled() -> bool:
    raw = (str(os.environ.get("TITAN_CONTEMP_DAMPENER_ENABLED", "")) or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _contemporaneous_move_pct(audit: dict[str, Any]) -> float:
    move = _sf(audit.get("session_move_vs_prev_close_pct"))
    if math.isnan(move):
        move = _sf(audit.get("return_1d_pct"))
    return move


def _contemporaneous_discount_factor(audit: dict[str, Any]) -> tuple[float, float]:
    if not _contemporaneous_dampener_enabled():
        return float("nan"), 0.0
    move = _contemporaneous_move_pct(audit)
    if math.isnan(move) or move <= 0.0:
        return move, 0.0
    threshold = _contemp_env_float("TITAN_CONTEMP_MOVE_THRESHOLD_PCT", _CONTEMP_MOVE_THRESHOLD_PCT)
    slope = _contemp_env_float("TITAN_CONTEMP_DISCOUNT_SLOPE", _CONTEMP_DISCOUNT_SLOPE)
    max_frac = _contemp_env_float("TITAN_CONTEMP_MAX_DISCOUNT_FRAC", _CONTEMP_MAX_DISCOUNT_FRAC)
    if move <= threshold:
        return move, 0.0
    frac = (move - threshold) * slope
    frac = max(0.0, min(max_frac, frac))
    return move, frac


def _high_volume_down_day_stress(audit: dict[str, Any]) -> bool:
    if audit.get("high_volume_down_day_proxy") or audit.get("panic_absorption_proxy"):
        return True
    ret1d = _sf(audit.get("return_1d_pct"))
    vpr = _sf(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))
    return not math.isnan(ret1d) and ret1d < 0.0 and not math.isnan(vpr) and vpr >= 1.5


def predictive_scores(
    audit: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """
    Heuristic lead scores (0-100) for next day / next week horizons.
    Uses effective_intent_score plus orthogonal horizon features.
    """
    w = {**PREDICTIVE_WEIGHT_DEFAULTS, **(weights or {})}

    tech = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    ret1d = _sf(audit.get("return_1d_pct"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ret10d = _sf(audit.get("return_10d_pct"))
    rel5 = _sf(audit.get("rel_return_5d_vs_nifty_pct"))
    rel20 = _sf(audit.get("rel_return_20d_vs_nifty_pct"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    atr_in = _sf(audit.get("atr_penalty_input"))
    if math.isnan(atr_in):
        atr_in = _sf(audit.get("atr_14_pct"))
    ema_conf = _ema_history_confidence(audit.get("rows"))

    tech_day = 0.0 if math.isnan(tech) else ((tech - 50.0) * w["tech_day"])
    tech_week = 0.0 if math.isnan(tech) else ((tech - 50.0) * w["tech_week"])
    ret1_w = 0.18 if audit.get("extreme_price_move_proxy") else w["ret1d"]
    ret_term = 0.0 if math.isnan(ret1d) else (ret1d * ret1_w)
    _contemp_move, _contemp_frac = _contemporaneous_discount_factor(audit)
    if _contemp_frac > 0.0 and ret_term > 0.0:
        ret_term *= (1.0 - _contemp_frac)
    ret5_term = 0.0 if math.isnan(ret5d) else (ret5d * w["ret5d"])
    ret10_term = 0.0 if math.isnan(ret10d) else (ret10d * w["ret10d"])
    rel5_term = 0.0 if math.isnan(rel5) else (rel5 * w["rel5"])
    rel20_term = 0.0 if math.isnan(rel20) else (rel20 * w["rel20"])
    ema_base = 0.0 if math.isnan(ema_dist) else (ema_dist * w["ema"] * ema_conf)
    ema_day = ema_base * 0.85
    ema_week = ema_base * 1.0
    atr_penalty = 0.0 if math.isnan(atr_in) else (atr_in * w["atr"])

    day_score = (
        50.0
        + tech_day
        + ret_term
        + ret5_term
        + 0.55 * ret10_term
        + rel5_term
        + 0.55 * rel20_term
        + ema_day
        - atr_penalty
    )
    week_score = (
        50.0
        + tech_week
        + (ret_term * 0.78)
        + 0.82 * ret5_term
        + 0.48 * ret10_term
        + 0.72 * rel5_term
        + 0.5 * rel20_term
        + ema_week
        - (atr_penalty * 0.35)
    )

    titan_blend = _contemp_env_float("TITAN_FUSION_PRED_BLEND", 0.0)
    titan_blend = max(0.0, min(1.0, titan_blend))
    titan = _sf(audit.get("titan_score"))
    if titan_blend > 0.0 and not math.isnan(titan):
        day_score = day_score * (1.0 - titan_blend) + titan * titan_blend
        week_score = week_score * (1.0 - titan_blend) + titan * titan_blend

    penalties: list[str] = []
    if audit.get("trap_exit_proxy"):
        day_score -= 8.0
        week_score -= 5.0
        penalties.append("trap_exit_proxy")
    if _high_volume_down_day_stress(audit):
        day_score -= 6.0
        week_score -= 4.0
        penalties.append("high_volume_down_day_stress")
    if audit.get("event_risk_soon"):
        day_score -= 4.0
        week_score -= 6.0
        penalties.append("event_risk_soon")

    breakdown = {
        "baseline": 50.0,
        "day": {
            "tech_composite_term": round(tech_day, 2),
            "ret1d_term": round(ret_term, 2),
            "ret5d_term": round(ret5_term, 2),
            "ret10d_term": round(0.55 * ret10_term, 2),
            "rel5_term": round(rel5_term, 2),
            "rel20_term": round(0.55 * rel20_term, 2),
            "ema_term": round(ema_day, 2),
            "ema_history_confidence": round(ema_conf, 2),
            "atr_penalty": round(atr_penalty, 2),
        },
        "week": {
            "tech_composite_term": round(tech_week, 2),
            "ret1d_term": round(ret_term * 0.78, 2),
            "ret5d_term": round(0.82 * ret5_term, 2),
            "ret10d_term": round(0.48 * ret10_term, 2),
            "rel5_term": round(0.72 * rel5_term, 2),
            "rel20_term": round(0.5 * rel20_term, 2),
            "ema_term": round(ema_week, 2),
            "ema_history_confidence": round(ema_conf, 2),
            "atr_penalty": round(atr_penalty * 0.35, 2),
        },
        "penalties": penalties,
        "contemporaneous_discount_fraction": round(_contemp_frac, 4),
        "contemporaneous_move_pct": (None if math.isnan(_contemp_move) else round(_contemp_move, 4)),
        "titan_fusion_pred_blend": round(titan_blend, 4),
        "titan_score_blend_input": (None if math.isnan(titan) else round(titan, 2)),
    }
    return _clamp_score(day_score), _clamp_score(week_score), breakdown
