"""Sector-wide equity audits: Supabase universe (CSV fallback), parallel Breeze fetches, digest email."""

from __future__ import annotations

import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig, load_config
from sector_registry import SectorInstrument, load_sector_instruments

logger = logging.getLogger(__name__)

# Parallel sector fetches (each worker opens its own Breeze session; keep modest for API limits).
MAX_WORKERS = 4

# Serialize Gemini calls so sector threads do not burst past rate limits together.
_GEMINI_SECTOR_LOCK = threading.Lock()
_FUNDAMENTAL_CACHE_LOCK = threading.Lock()
_FUNDAMENTAL_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_PARTICIPATION_CALIBRATION_LOCK = threading.Lock()
_PARTICIPATION_CALIBRATION_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_THREAD_LOCAL = threading.local()
IST = ZoneInfo("Asia/Kolkata")
# Volume participation ratio caps (historical column in DB may still be named absorption_ratio).
PARTICIPATION_CAP_DEFAULT = 2.5
PARTICIPATION_CAP_MIN_HISTORY = 8
PARTICIPATION_CAP_LOOKBACK = 45
PARTICIPATION_CAP_PERCENTILE = 90.0
PARTICIPATION_CAP_MAX = 5.0
ABSORPTION_CAP_DEFAULT = PARTICIPATION_CAP_DEFAULT
ABSORPTION_CAP_MIN_HISTORY = PARTICIPATION_CAP_MIN_HISTORY
ABSORPTION_CAP_LOOKBACK = PARTICIPATION_CAP_LOOKBACK
ABSORPTION_CAP_PERCENTILE = PARTICIPATION_CAP_PERCENTILE
ABSORPTION_CAP_MAX = PARTICIPATION_CAP_MAX


def _fmt_metric(x: Any, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(v):
        return "n/a"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return f"{v:.{digits}f}"


def _z_label(z: Any) -> str:
    try:
        v = float(z)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 2.0:
        return "strong bullish deviation"
    if v >= 1.0:
        return "bullish deviation"
    if v <= -2.0:
        return "strong bearish deviation"
    if v <= -1.0:
        return "bearish deviation"
    return "near mean"


def _volume_participation_label(vpr: Any) -> str:
    try:
        v = float(vpr)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if math.isinf(v) and v > 0:
        return "extreme relative volume"
    if v >= 1.5:
        return "high participation"
    if v >= 1.0:
        return "above average participation"
    if v >= 0.7:
        return "below average participation"
    return "thin participation"


def _equity_technical_label(score: Any) -> str:
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 70:
        return "high conviction long bias"
    if v >= 55:
        return "moderate long bias"
    if v >= 45:
        return "balanced / neutral"
    if v >= 30:
        return "moderate defensive bias"
    return "high defensive bias"


def _digest_verbose_symbol_lines_enabled() -> bool:
    """Long single-line payload for power users / debugging (default: short blocks)."""
    return (os.environ.get("TITAN_DIGEST_VERBOSE_SYMBOLS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _sell_signal_plain_english(signal: str) -> str:
    s = str(signal or "").strip().lower().replace("_", "-")
    return {
        # Risk score bands from _derive_sell_signal: trim 4–6.99, exit-risk ≥7
        "trim": "TRIM — risk score 4–6: lighten / take profits (below hard-exit bar)",
        "hold": "HOLD — risk score <4: no strong defensive trigger",
        "exit-risk": "EXIT RISK — risk score ≥7: hard exit bar — cut exposure sharply or exit",
    }.get(s, signal or "Review")


def _digest_flags_simple(audit: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if audit.get("panic_absorption_proxy"):
        out.append("heavy volume on a down day")
    if audit.get("trap_exit_proxy"):
        out.append("trap-exit style move")
    if audit.get("cluster_guardrail_applied"):
        out.append("sector breadth weakness")
    if audit.get("macro_guardrail_applied"):
        out.append("macro risk throttle")
    if audit.get("event_risk_soon"):
        out.append("event risk within ~3 sessions")
    return out


def _prediction_brief_line(audit: dict[str, Any]) -> str:
    """One readable line instead of raw factor vectors."""
    breakdown = audit.get("prediction_breakdown")
    if not isinstance(breakdown, dict):
        return ""
    week = breakdown.get("week", {}) if isinstance(breakdown.get("week"), dict) else {}
    penalties = breakdown.get("penalties") if isinstance(breakdown.get("penalties"), list) else []
    next_week = _safe_float(audit.get("next_week_score"))
    if math.isnan(next_week):
        confidence = "unknown"
    elif next_week >= 70:
        confidence = "high"
    elif next_week >= 55:
        confidence = "medium"
    else:
        confidence = "low"
    if penalties and confidence == "high":
        confidence = "medium"
    elif penalties and confidence == "medium":
        confidence = "low"

    contributors = [
        ("structure", _safe_float(week.get("tech_composite_term"))),
        ("trend", _safe_float(week.get("ema_term"))),
        ("momentum", _safe_float(week.get("ret1d_term"))),
        ("volatility", -_safe_float(week.get("atr_penalty"))),
    ]
    drivers = [name for name, val in contributors if not math.isnan(val) and val >= 1.0]
    drags = [name for name, val in contributors if not math.isnan(val) and val <= -1.0]
    atr_pen = _safe_float(week.get("atr_penalty"))
    if not math.isnan(atr_pen) and atr_pen >= 1.0:
        drags.append("volatility")
    drv = drivers[0] if drivers else None
    drag = drags[0] if drags else None

    parts = [f"Model read: {confidence} confidence"]
    if drv:
        parts.append(f"{drv} supportive")
    if drag:
        parts.append(f"{drag} weighing on the score")
    if penalties:
        parts.append(f"flags: {'; '.join(str(p) for p in penalties[:2])}")
    return " · ".join(parts)


def _format_symbol_metrics_line_simple(result: dict[str, Any]) -> str:
    symbol = result["symbol"]
    exchange = result["exchange"]
    audit = result["audit"]
    z = audit.get("z_score")
    intent = audit.get("effective_intent_score", audit.get("intent_score"))
    vpr = audit.get("volume_participation_ratio", audit.get("absorption_ratio"))
    ret1d = audit.get("return_1d_pct")
    atr_pct = audit.get("atr_14_pct")
    next_week = audit.get("next_week_score")
    ema_dist = audit.get("ema_200_distance_pct")
    fundamental_status = str(audit.get("fundamental_status", "unavailable") or "unavailable")
    fundamental_score = audit.get("fundamental_score")
    fundamental_reasons = audit.get("fundamental_reasons") if isinstance(audit.get("fundamental_reasons"), list) else []
    support_tag = audit.get("hypothesis_support", "technical_only")
    sell_signal = audit.get("sell_signal", "unknown")
    sell_reasons = audit.get("sell_signal_reasons") if isinstance(audit.get("sell_signal_reasons"), list) else []
    exchange_used = str(audit.get("exchange_used", exchange))
    fallback_used = bool(audit.get("exchange_fallback_used", False))
    nf = audit.get("next_day_score")

    lines_out: list[str] = []
    lines_out.append(f"{symbol} ({exchange}) — {_sell_signal_plain_english(str(sell_signal))}")
    lines_out.append(
        "Scores · "
        f"next week {_fmt_metric(next_week)} · "
        f"intent {_fmt_metric(intent)} ({_equity_technical_label(intent)}) · "
        f"1d {_fmt_metric(ret1d)}%"
    )

    tape_bits = [
        f"z {_fmt_metric(z)} ({_z_label(z)})",
        f"volume {_volume_participation_label(vpr)}",
        f"ATR {_fmt_metric(atr_pct)}%",
    ]
    if not math.isnan(_safe_float(ema_dist)):
        tape_bits.insert(2, f"vs 200-day avg {_fmt_metric(ema_dist)}%")
    lines_out.append("Tape · " + " · ".join(tape_bits))

    if not math.isnan(_safe_float(nf)):
        lines_out.append(f"Very short horizon: tomorrow ~{_fmt_metric(nf)}")

    if sell_reasons:
        sr = "; ".join(str(x) for x in sell_reasons[:3])
        lines_out.append(f"Why this action: {sr}")

    pred = _prediction_brief_line(audit)
    if pred:
        lines_out.append(pred)

    flag_simple = _digest_flags_simple(audit)
    if flag_simple:
        lines_out.append("Context: " + "; ".join(flag_simple))

    if support_tag != "technical_only":
        lines_out.append(f"Evidence mix: {support_tag.replace('_', ' ')}")

    if fundamental_status.lower() not in ("unavailable", "na", "n/a", "") and not str(fundamental_status).startswith(
        "unavailable",
    ):
        fr = "; ".join(str(x) for x in fundamental_reasons[:2]) if fundamental_reasons else ""
        lines_out.append(
            f"Fundamentals: {fundamental_status} ({_fmt_metric(fundamental_score)})"
            + (f" — {fr}" if fr else ""),
        )

    if fallback_used and exchange_used.upper() != str(exchange).upper():
        lines_out.append(f"Price feed: pulled from {exchange_used} (alternate to {exchange}).")

    head = lines_out[0]
    tail = ["  " + ln for ln in lines_out[1:]]
    return "\n".join([head, *tail])


def _format_symbol_metrics_line_verbose(result: dict[str, Any]) -> str:
    symbol = result["symbol"]
    exchange = result["exchange"]
    audit = result["audit"]
    z = audit.get("z_score")
    intent = audit.get("effective_intent_score", audit.get("intent_score"))
    vpr = audit.get("volume_participation_ratio", audit.get("absorption_ratio"))
    vpr_score = audit.get("volume_participation_for_scoring", audit.get("absorption_for_scoring"))
    ret1d = audit.get("return_1d_pct")
    ema_dist = audit.get("ema_200_distance_pct")
    atr_pct = audit.get("atr_14_pct")
    next_day = audit.get("next_day_score")
    next_week = audit.get("next_week_score")
    fundamental_status = audit.get("fundamental_status", "unavailable")
    fundamental_score = audit.get("fundamental_score")
    fundamental_reasons = audit.get("fundamental_reasons") if isinstance(audit.get("fundamental_reasons"), list) else []
    support_tag = audit.get("hypothesis_support", "technical_only")
    sell_signal = audit.get("sell_signal", "unknown")
    sell_reasons = audit.get("sell_signal_reasons") if isinstance(audit.get("sell_signal_reasons"), list) else []
    calibration = audit.get("volume_participation_calibration", audit.get("absorption_calibration"))
    calibration = calibration if isinstance(calibration, dict) else {}
    verbose_vpr = (os.environ.get("TITAN_DIGEST_VERBOSE_VPR") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    vpr_cap_suffix = ""
    if verbose_vpr and calibration:
        cap = calibration.get("cap")
        cap_method = calibration.get("method", "n/a")
        vpr_cap_suffix = f" | volPartCap {_fmt_metric(cap, 2)} ({cap_method})"
    rows = audit.get("rows")
    exchange_used = str(audit.get("exchange_used", exchange))
    fallback_used = bool(audit.get("exchange_fallback_used", False))
    flags: list[str] = []
    if audit.get("panic_absorption_proxy"):
        flags.append("panic-volume-on-down-day")
    if audit.get("trap_exit_proxy"):
        flags.append("up-move-trap")
    if audit.get("cluster_guardrail_applied"):
        flags.append("cluster-downgraded")
    if audit.get("macro_guardrail_applied"):
        flags.append("macro-risk-throttle")
    if audit.get("event_risk_soon"):
        flags.append("event-risk<=3d")
    flag_text = ", ".join(flags) if flags else "none"
    fundamental_text = (
        f"{fundamental_status}:{_fmt_metric(fundamental_score)}"
        + (f" ({'; '.join(str(x) for x in fundamental_reasons[:2])})" if fundamental_reasons else "")
    )
    base = (
        f"{symbol} ({exchange}) | techScore {_fmt_metric(intent)} [{_equity_technical_label(intent)}] "
        f"| z {_fmt_metric(z)} [{_z_label(z)}] | volPart {_fmt_metric(vpr, 3)} "
        f"[{_volume_participation_label(vpr)}] | volPartScore {_fmt_metric(vpr_score, 2)}"
        f"{vpr_cap_suffix} "
        f"| ret1d {_fmt_metric(ret1d)}% "
        f"| ema200_delta {_fmt_metric(ema_dist)}% | atr14 {_fmt_metric(atr_pct)}% "
        f"| nextDay {_fmt_metric(next_day)} | nextWeek {_fmt_metric(next_week)} "
        f"| sell {sell_signal} "
        f"| flags={flag_text} | rows {rows} "
        f"| exchange_used={exchange_used}{' (fallback)' if fallback_used else ''}"
    )
    sell_reason_text = (
        f" ({'; '.join(str(x) for x in sell_reasons[:2])})" if sell_reasons else ""
    )
    return (
        f"{base} | support={support_tag} | fundamentals={fundamental_text} "
        f"| sellReason={sell_signal}{sell_reason_text} | {_prediction_reason_text(audit)}"
    )


def _format_symbol_metrics_line(result: dict[str, Any]) -> str:
    if _digest_verbose_symbol_lines_enabled():
        return _format_symbol_metrics_line_verbose(result)
    return _format_symbol_metrics_line_simple(result)


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _percentile(values: list[float], pct: float) -> float:
    xs = sorted(x for x in values if not math.isnan(x))
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    rank = (max(0.0, min(100.0, float(pct))) / 100.0) * (len(xs) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return xs[lo]
    frac = rank - lo
    return xs[lo] + ((xs[hi] - xs[lo]) * frac)


def _recent_volume_participation_samples(cfg: TitanConfig, inst: SectorInstrument) -> list[float]:
    """Historical VPR samples from Supabase (column may still be named absorption_ratio)."""
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("symbol_daily_features")
            .select("absorption_ratio")
            .eq("symbol", inst.symbol)
            .eq("exchange", inst.exchange)
            .order("trade_date", desc=True)
            .limit(PARTICIPATION_CAP_LOOKBACK)
            .execute()
        )
    except Exception:
        return []
    rows = list(getattr(res, "data", None) or [])
    out: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        v = _safe_float(row.get("absorption_ratio"))
        if math.isnan(v) or math.isinf(v) or v <= 0.0:
            continue
        out.append(v)
    return out


def _resolve_participation_cap(cfg: TitanConfig, inst: SectorInstrument) -> dict[str, Any]:
    key = (inst.symbol, inst.exchange)
    with _PARTICIPATION_CALIBRATION_LOCK:
        cached = _PARTICIPATION_CALIBRATION_CACHE.get(key)
    if cached is not None:
        return dict(cached)

    samples = _recent_absorption_samples(cfg, inst)
    pct_label = int(PARTICIPATION_CAP_PERCENTILE)
    method = "fallback_default"
    cap = PARTICIPATION_CAP_DEFAULT
    if len(samples) >= PARTICIPATION_CAP_MIN_HISTORY:
        pctl = _percentile(samples, PARTICIPATION_CAP_PERCENTILE)
        if not math.isnan(pctl):
            cap = max(1.0, min(PARTICIPATION_CAP_MAX, pctl))
            method = f"symbol_daily_features_p{pct_label}"
    out = {
        "method": method,
        "cap": round(cap, 4),
        "sample_count": len(samples),
        "lookback": PARTICIPATION_CAP_LOOKBACK,
        "percentile": pct_label,
    }
    with _PARTICIPATION_CALIBRATION_LOCK:
        _PARTICIPATION_CALIBRATION_CACHE[key] = dict(out)
    return out


# Back-compat names for tests and older call sites
_recent_absorption_samples = _recent_volume_participation_samples
_resolve_absorption_cap = _resolve_participation_cap


def _is_skipped_no_data_error(error: Any) -> bool:
    msg = str(error or "").lower()
    return ("no rows returned" in msg and "skipped" in msg) or "skipped_no_data" in msg


def _classify_error_code(error: Any) -> str:
    msg = str(error or "").lower()
    if _is_skipped_no_data_error(msg):
        return "no_data_skipped"
    if "historical fetch failed after retries" in msg:
        return "data_fetch_failed"
    if "session token expired" in msg or "auth/permission" in msg:
        return "auth_or_session"
    return "runtime_error"


def _normalize_participation_for_scoring(participation_raw: float) -> float:
    """
    Compress extreme volume-participation outliers for scoring (single smooth map).

    Raw VPR is unbounded ratio; this maps to a bounded score input (~0..3) so
    one illiquid spike cannot dominate ``calculate_equity_technical_score`` and
    predictive outputs.
    """
    v = _safe_float(participation_raw)
    if math.isnan(v):
        return float("nan")
    if math.isinf(v) and v > 0:
        return 3.0
    if v <= 0.0:
        return 0.0
    return min(3.0, (math.log1p(v) / math.log(4.0)) * 3.0)


_normalize_absorption_for_scoring = _normalize_participation_for_scoring


def _calibrate_volume_participation_v2(
    cfg: TitanConfig,
    inst: SectorInstrument,
    vpr_raw: float,
) -> tuple[float, float, dict[str, Any]]:
    meta = _resolve_participation_cap(cfg, inst)
    raw = _safe_float(vpr_raw)
    cap = _safe_float(meta.get("cap"))
    if math.isnan(cap) or cap <= 0.0:
        cap = PARTICIPATION_CAP_DEFAULT
        meta = {**meta, "method": "fallback_default", "cap": cap}

    if math.isnan(raw):
        calibrated_raw = float("nan")
    elif raw <= 0.0:
        calibrated_raw = 0.0
    elif math.isinf(raw) and raw > 0.0:
        calibrated_raw = cap
    else:
        calibrated_raw = min(raw, cap)
    calibrated_for_scoring = _normalize_participation_for_scoring(calibrated_raw)
    return calibrated_raw, calibrated_for_scoring, {
        **meta,
        "raw": raw,
        "calibrated_raw": calibrated_raw,
    }


_calibrate_absorption_v2 = _calibrate_volume_participation_v2


def _thread_breeze_session(cfg: TitanConfig) -> Any:
    from breeze_client import create_breeze_session

    breeze = getattr(_THREAD_LOCAL, "breeze", None)
    token = getattr(_THREAD_LOCAL, "breeze_token", None)
    if breeze is None or token != cfg.breeze_session_token:
        breeze = create_breeze_session(cfg)
        _THREAD_LOCAL.breeze = breeze
        _THREAD_LOCAL.breeze_token = cfg.breeze_session_token
    return breeze


def _clamp_score(x: float) -> float:
    return max(0.0, min(100.0, round(x, 2)))


def _ema_history_confidence(rows: Any) -> float:
    """Down-weight EMA200 distance when we do not have ~200 sessions yet (Phase B)."""
    try:
        n = int(rows)
    except (TypeError, ValueError):
        return 1.0
    if n < 30:
        return 0.35
    return min(1.0, max(0.35, n / 200.0))


def _predictive_scores(audit: dict[str, Any]) -> tuple[float, float, dict[str, Any]]:
    """
    Heuristic lead scores (0-100).

    Uses a **single** cash-market composite (``effective_intent_score`` = z + volume
    participation via ``calculate_equity_technical_score``) plus **orthogonal**
    horizon features (1d return, EMA200 distance with history confidence, ATR).
    Avoids double-counting z/participation alongside that composite.
    """
    tech = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    ret1d = _safe_float(audit.get("return_1d_pct"))
    ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
    atr_pct = _safe_float(audit.get("atr_14_pct"))
    ema_conf = _ema_history_confidence(audit.get("rows"))

    tech_day = 0.0 if math.isnan(tech) else ((tech - 50.0) * 0.52)
    tech_week = 0.0 if math.isnan(tech) else ((tech - 50.0) * 0.62)
    ret_term = 0.0 if math.isnan(ret1d) else (ret1d * 0.55)
    ema_base = 0.0 if math.isnan(ema_dist) else (ema_dist * 0.26 * ema_conf)
    ema_day = ema_base * 0.85
    ema_week = ema_base * 1.0
    atr_penalty = 0.0 if math.isnan(atr_pct) else (atr_pct * 0.45)

    day_score = 50.0 + tech_day + ret_term + ema_day - atr_penalty
    week_score = 50.0 + tech_week + (ret_term * 0.82) + ema_week - (atr_penalty * 0.35)

    penalties: list[str] = []
    if audit.get("trap_exit_proxy"):
        day_score -= 8.0
        week_score -= 5.0
        penalties.append("trap_exit_proxy")
    if audit.get("panic_absorption_proxy"):
        day_score -= 6.0
        week_score -= 4.0
        penalties.append("panic_volume_participation_proxy")
    if audit.get("event_risk_soon"):
        day_score -= 4.0
        week_score -= 6.0
        penalties.append("event_risk_soon")

    breakdown = {
        "baseline": 50.0,
        "day": {
            "tech_composite_term": round(tech_day, 2),
            "ret1d_term": round(ret_term, 2),
            "ema_term": round(ema_day, 2),
            "ema_history_confidence": round(ema_conf, 2),
            "atr_penalty": round(atr_penalty, 2),
        },
        "week": {
            "tech_composite_term": round(tech_week, 2),
            "ret1d_term": round(ret_term * 0.82, 2),
            "ema_term": round(ema_week, 2),
            "ema_history_confidence": round(ema_conf, 2),
            "atr_penalty": round(atr_penalty * 0.35, 2),
        },
        "penalties": penalties,
    }
    return _clamp_score(day_score), _clamp_score(week_score), breakdown


def _bucket_name(audit: dict[str, Any]) -> str:
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ab = _safe_float(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))
    if audit.get("trap_exit_proxy"):
        return "trap-risk"
    if (not math.isnan(eff) and eff >= 65.0) and (not math.isnan(z) and z >= 2.0) and (
        not math.isnan(ab) and ab >= 1.0
    ):
        return "high-conviction-momentum"
    if (not math.isnan(eff) and eff >= 55.0) or (not math.isnan(z) and z >= 1.5):
        return "constructive-watchlist"
    return "neutral-weak"


def _short_reason(audit: dict[str, Any]) -> str:
    z = _safe_float(audit.get("z_score"))
    ab = _safe_float(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))
    bits: list[str] = []
    if not math.isnan(z):
        bits.append(f"z={z:.2f}")
    if not math.isnan(ab):
        bits.append(f"vpr={ab:.2f}")
    if audit.get("trap_exit_proxy"):
        bits.append("trap-flag")
    if audit.get("macro_guardrail_applied"):
        bits.append("macro-throttle")
    return ", ".join(bits[:3]) if bits else "insufficient data"


def _prediction_reason_text(audit: dict[str, Any]) -> str:
    breakdown = audit.get("prediction_breakdown")
    if not isinstance(breakdown, dict):
        return "prediction factors unavailable"
    day = breakdown.get("day", {}) if isinstance(breakdown.get("day"), dict) else {}
    week = breakdown.get("week", {}) if isinstance(breakdown.get("week"), dict) else {}
    penalties = breakdown.get("penalties") if isinstance(breakdown.get("penalties"), list) else []
    pen = ",".join(str(x) for x in penalties) if penalties else "none"

    next_week = _safe_float(audit.get("next_week_score"))
    if math.isnan(next_week):
        confidence = "unknown"
    elif next_week >= 70:
        confidence = "high"
    elif next_week >= 55:
        confidence = "medium"
    else:
        confidence = "low"
    if penalties and confidence == "high":
        confidence = "medium"
    elif penalties and confidence == "medium":
        confidence = "low"

    contributors = [
        ("techComposite", _safe_float(week.get("tech_composite_term"))),
        ("trend", _safe_float(week.get("ema_term"))),
        ("momentum", _safe_float(week.get("ret1d_term"))),
        ("volatility", -_safe_float(week.get("atr_penalty"))),
    ]
    drivers = [name for name, val in contributors if not math.isnan(val) and val >= 1.0]
    drags = [name for name, val in contributors if not math.isnan(val) and val <= -1.0]
    atr_pen = _safe_float(week.get("atr_penalty"))
    if not math.isnan(atr_pen) and atr_pen >= 1.0:
        drags.append("atr-drag")
    if not drivers:
        drivers = ["none"]
    if not drags:
        drags = ["none"]

    return (
        f"confidence={confidence} | drivers={','.join(drivers[:3])} | drags={','.join(drags[:3])} | penalties={pen} "
        f"| factors day[tech {_fmt_metric(day.get('tech_composite_term'))}, "
        f"ret {_fmt_metric(day.get('ret1d_term'))}, ema {_fmt_metric(day.get('ema_term'))}, "
        f"emaConf {_fmt_metric(day.get('ema_history_confidence'))}, atr-pen {_fmt_metric(day.get('atr_penalty'))}] "
        f"week[tech {_fmt_metric(week.get('tech_composite_term'))}, "
        f"ret {_fmt_metric(week.get('ret1d_term'))}, ema {_fmt_metric(week.get('ema_term'))}, "
        f"emaConf {_fmt_metric(week.get('ema_history_confidence'))}, atr-pen {_fmt_metric(week.get('atr_penalty'))}]"
    )


def _derive_sell_signal(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    risk = 0.0
    reasons: list[str] = []

    def add(points: float, reason: str) -> None:
        nonlocal risk
        risk += points
        reasons.append(reason)

    next_week = _safe_float(audit.get("next_week_score"))
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ret1d = _safe_float(audit.get("return_1d_pct"))
    ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
    atr_pct = _safe_float(audit.get("atr_14_pct"))

    if not math.isnan(next_week):
        if next_week < 45.0:
            add(3.0, f"nextWeek weak {_fmt_metric(next_week)}")
        elif next_week < 55.0:
            add(1.0, f"nextWeek soft {_fmt_metric(next_week)}")
    if not math.isnan(eff):
        if eff < 45.0:
            add(2.0, f"intent defensive {_fmt_metric(eff)}")
        elif eff < 52.0:
            add(1.0, f"intent cooling {_fmt_metric(eff)}")
    if not math.isnan(z):
        if z <= -2.0:
            add(2.0, f"z bearish {_fmt_metric(z)}")
        elif z <= -1.0:
            add(1.0, f"z below mean {_fmt_metric(z)}")
    if not math.isnan(ret1d):
        if ret1d <= -2.0:
            add(2.0, f"1d return weak {_fmt_metric(ret1d)}%")
        elif ret1d <= -1.0:
            add(1.0, f"1d return soft {_fmt_metric(ret1d)}%")
    if not math.isnan(ema_dist):
        if ema_dist <= -6.0:
            add(2.0, f"below ema200 {_fmt_metric(ema_dist)}%")
        elif ema_dist <= -2.0:
            add(1.0, f"ema200 drift {_fmt_metric(ema_dist)}%")
    if not math.isnan(atr_pct):
        if atr_pct >= 6.0:
            add(2.0, f"atr elevated {_fmt_metric(atr_pct)}%")
        elif atr_pct >= 4.0:
            add(1.0, f"atr high {_fmt_metric(atr_pct)}%")

    if audit.get("trap_exit_proxy"):
        add(2.0, "trap-exit proxy")
    if audit.get("panic_absorption_proxy"):
        add(1.0, "panic volume-participation proxy")
    if audit.get("cluster_guardrail_applied"):
        add(1.0, "cluster guardrail")
    if audit.get("macro_guardrail_applied"):
        add(1.0, "macro guardrail")
    if audit.get("event_guardrail_applied") or audit.get("event_risk_soon"):
        add(1.0, "event risk")

    f_status = str(audit.get("fundamental_status") or "unavailable")
    if f_status == "weak":
        add(2.0, "fundamentals weak")
    elif f_status == "balanced":
        add(1.0, "fundamentals balanced")
    elif f_status == "strong":
        risk = max(0.0, risk - 1.0)

    risk = round(risk, 2)
    if risk >= 7.0:
        signal = "exit-risk"
    elif risk >= 4.0:
        signal = "trim"
    else:
        signal = "hold"
    if not reasons:
        reasons = ["technical+risk profile stable"]
    return signal, risk, reasons[:4]


def _refresh_symbol_scoring_outputs(audit: dict[str, Any]) -> None:
    next_day_score, next_week_score, prediction_breakdown = _predictive_scores(audit)
    audit["next_day_score"] = next_day_score
    audit["next_week_score"] = next_week_score
    audit["prediction_breakdown"] = prediction_breakdown
    audit["hypothesis_support"] = _hypothesis_support_tag(audit)
    sell_signal, sell_risk_score, sell_reasons = _derive_sell_signal(audit)
    audit["sell_signal"] = sell_signal
    audit["sell_signal_risk_score"] = sell_risk_score
    audit["sell_signal_reasons"] = sell_reasons


def _first_float_field(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        if k in row:
            v = _safe_float(row.get(k))
            if not math.isnan(v):
                return v
    return float("nan")


def _assess_fundamental_strength(cfg: TitanConfig, inst: SectorInstrument) -> dict[str, Any]:
    cache_key = (inst.symbol, inst.exchange)
    with _FUNDAMENTAL_CACHE_LOCK:
        cached = _FUNDAMENTAL_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("market_instruments")
            .select("*")
            .eq("symbol", inst.symbol)
            .eq("exchange", inst.exchange)
            .limit(1)
            .execute()
        )
    except Exception:
        out = {"status": "unavailable", "score": None, "reasons": ["fundamental lookup unavailable"]}
        with _FUNDAMENTAL_CACHE_LOCK:
            _FUNDAMENTAL_CACHE[cache_key] = dict(out)
        return out

    rows = list(getattr(res, "data", None) or [])
    if not rows or not isinstance(rows[0], dict):
        out = {"status": "unavailable", "score": None, "reasons": ["fundamental row missing"]}
        with _FUNDAMENTAL_CACHE_LOCK:
            _FUNDAMENTAL_CACHE[cache_key] = dict(out)
        return out

    row = rows[0]
    roe = _first_float_field(row, ("roe", "roe_pct", "return_on_equity", "return_on_equity_pct"))
    roce = _first_float_field(row, ("roce", "roce_pct", "return_on_capital", "return_on_capital_employed"))
    de = _first_float_field(row, ("debt_to_equity", "de_ratio", "debt_equity"))
    margin = _first_float_field(row, ("net_profit_margin", "npm", "operating_margin", "opm"))
    score = 50.0
    reasons: list[str] = []
    used = 0

    if not math.isnan(roe):
        used += 1
        if roe >= 15.0:
            score += 12.0
            reasons.append(f"ROE strong {_fmt_metric(roe)}")
        elif roe >= 10.0:
            score += 6.0
            reasons.append(f"ROE acceptable {_fmt_metric(roe)}")
        elif roe < 5.0:
            score -= 8.0
            reasons.append(f"ROE weak {_fmt_metric(roe)}")
    if not math.isnan(roce):
        used += 1
        if roce >= 15.0:
            score += 10.0
            reasons.append(f"ROCE strong {_fmt_metric(roce)}")
        elif roce >= 10.0:
            score += 5.0
            reasons.append(f"ROCE acceptable {_fmt_metric(roce)}")
        elif roce < 6.0:
            score -= 6.0
            reasons.append(f"ROCE weak {_fmt_metric(roce)}")
    if not math.isnan(de):
        used += 1
        if de <= 0.5:
            score += 8.0
            reasons.append(f"debt/equity low {_fmt_metric(de)}")
        elif de <= 1.0:
            score += 4.0
            reasons.append(f"debt/equity moderate {_fmt_metric(de)}")
        elif de > 2.0:
            score -= 8.0
            reasons.append(f"debt/equity high {_fmt_metric(de)}")
    if not math.isnan(margin):
        used += 1
        if margin >= 12.0:
            score += 6.0
            reasons.append(f"margin strong {_fmt_metric(margin)}")
        elif margin < 3.0:
            score -= 4.0
            reasons.append(f"margin thin {_fmt_metric(margin)}")

    if used == 0:
        out = {"status": "unavailable", "score": None, "reasons": ["fundamental fields unavailable"]}
    else:
        s = _clamp_score(score)
        if s >= 65.0:
            st = "strong"
        elif s <= 40.0:
            st = "weak"
        else:
            st = "balanced"
        out = {"status": st, "score": s, "reasons": reasons[:3]}

    with _FUNDAMENTAL_CACHE_LOCK:
        _FUNDAMENTAL_CACHE[cache_key] = dict(out)
    return out


def _hypothesis_support_tag(audit: dict[str, Any]) -> str:
    next_week = _safe_float(audit.get("next_week_score"))
    f_status = str(audit.get("fundamental_status") or "unavailable")
    if f_status == "unavailable":
        return "technical_only"
    if not math.isnan(next_week) and next_week >= 70.0 and f_status == "strong":
        return "strongly_supported"
    if not math.isnan(next_week) and next_week >= 60.0 and f_status in ("strong", "balanced"):
        return "partially_supported"
    return "low_support"


def _apply_cluster_guardrails(ok_results: list[dict[str, Any]]) -> tuple[float, int]:
    if not ok_results:
        return 0.0, 0
    red_count = 0
    for r in ok_results:
        ret = _safe_float(r["audit"].get("return_1d_pct"))
        if not math.isnan(ret) and ret <= -1.0:
            red_count += 1
    red_ratio = red_count / len(ok_results)
    applied = 0
    if red_ratio > 0.70:
        for r in ok_results:
            a = r["audit"]
            intent = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
            if not math.isnan(intent) and intent >= 55.0:
                a["effective_intent_score"] = min(intent, 50.0)
                a["cluster_guardrail_applied"] = True
                a["cluster_guardrail_reason"] = (
                    f"cluster breadth risk: {red_count}/{len(ok_results)} names <= -1% day return"
                )
                applied += 1
    return red_ratio, applied


def _apply_macro_guardrails(
    ok_results: list[dict[str, Any]], macro_snapshot: dict[str, Any] | None
) -> tuple[bool, str]:
    if not macro_snapshot:
        return False, "macro snapshot not provided"
    gift = _safe_float(macro_snapshot.get("gift_nifty_change_pct"))
    vix = _safe_float(macro_snapshot.get("india_vix"))
    risk_on = (not math.isnan(gift) and gift < -0.5) or (not math.isnan(vix) and vix > 18.0)
    if not risk_on:
        return False, "macro trigger not active"
    for r in ok_results:
        a = r["audit"]
        base = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
        if math.isnan(base):
            continue
        a["effective_intent_score"] = round(base * 0.5, 2)
        a["macro_guardrail_applied"] = True
        a["macro_guardrail_reason"] = (
            f"GIFT={_fmt_metric(gift)}%, IndiaVIX={_fmt_metric(vix)} (trigger: GIFT<-0.5 or VIX>18)"
        )
    return True, f"GIFT={_fmt_metric(gift)}%, IndiaVIX={_fmt_metric(vix)}"


def _apply_event_guardrails(ok_results: list[dict[str, Any]]) -> int:
    adjusted = 0
    for r in ok_results:
        a = r["audit"]
        if not a.get("event_risk_soon"):
            continue
        base = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
        if math.isnan(base):
            continue
        a["effective_intent_score"] = round(base * 0.85, 2)
        a["event_guardrail_applied"] = True
        adjusted += 1
    return adjusted


def _prediction_quality_gate(
    ok_results: list[dict[str, Any]],
    *,
    total_count: int,
) -> dict[str, Any]:
    """
    Conservative deployment gate for predictive scoring quality.

    This is a safety gate (confidence/coverage), not a promise of market prediction certainty.
    """
    audits = [r.get("audit") for r in ok_results if isinstance(r.get("audit"), dict)]
    next_week_vals = [
        _safe_float(a.get("next_week_score"))
        for a in audits
        if not math.isnan(_safe_float(a.get("next_week_score")))
    ]
    coverage_ratio = (len(ok_results) / total_count) if total_count else 0.0
    scored_ratio = (len(next_week_vals) / len(audits)) if audits else 0.0
    sorted_vals = sorted(next_week_vals, reverse=True)
    top5 = sorted_vals[:5]
    top5_mean = (sum(top5) / len(top5)) if top5 else float("nan")
    spread_top_bottom = (sorted_vals[0] - sorted_vals[-1]) if len(sorted_vals) >= 2 else float("nan")

    reasons: list[str] = []
    if len(ok_results) < 10:
        reasons.append("insufficient successful symbols (<10)")
    if coverage_ratio < 0.60:
        reasons.append("coverage below 60%")
    if scored_ratio < 0.90:
        reasons.append("predictive score coverage below 90%")
    if math.isnan(top5_mean) or top5_mean < 55.0:
        reasons.append("top-5 next-week score mean below 55")
    if math.isnan(spread_top_bottom) or spread_top_bottom < 8.0:
        reasons.append("signal spread too narrow (<8 points)")

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "ok_count": len(ok_results),
        "total_count": total_count,
        "coverage_ratio": coverage_ratio,
        "scored_ratio": scored_ratio,
        "top5_next_week_mean": top5_mean,
        "spread_top_bottom": spread_top_bottom,
    }


def build_equity_live_audit(
    cfg: TitanConfig,
    breeze: Any,
    inst: SectorInstrument,
    *,
    sector_id: str,
    lookback_calendar_days: int = 60,
    with_narrative: bool = True,
    strict_data: bool = False,
    event_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Cash-market metrics: z-score, **volume participation ratio** (VPR), equity technical
    score (z + VPR, no dummy PCR), ATR/EMA context. Option chain / PCR are skipped for
    sector equities (``option_chain_unavailable``).

    FII/DII **institutional** buy/sell by stock is not available from Breeze cash OHLC;
    see ``institutional_flow`` on the audit payload for ingestion status.

    With default ``strict_data=False``, empty Breeze history returns ``skipped_no_data`` instead of
    raising (sector runs skip that symbol). Pass ``strict_data=True`` to fail hard on no rows.
    """
    import pandas as pd

    from breeze_client import fetch_equity_data, volume_participation_ratio
    from titan_engine import (
        calculate_atr,
        calculate_ema,
        calculate_equity_technical_score,
        calculate_z_score,
    )

    df = fetch_equity_data(
        cfg,
        inst.symbol,
        inst.exchange,
        breeze=breeze,
        lookback_calendar_days=lookback_calendar_days,
    )
    exchange_used = str(df.attrs.get("exchange_used", inst.exchange)).strip().upper()
    fallback_used = bool(df.attrs.get("exchange_fallback_used", False))
    if df.empty:
        if strict_data:
            raise RuntimeError(
                f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); task BLOCKED"
            )
        skip: dict[str, Any] = {
            "benchmark": "equity",
            "sector_mode": True,
            "sector": sector_id,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "exchange_used": exchange_used or inst.exchange,
            "exchange_fallback_used": fallback_used,
            "skipped_no_data": True,
            "z_score": float("nan"),
            "volume_participation_ratio": float("nan"),
            "absorption_ratio": float("nan"),
            "pcr": float("nan"),
            "put_oi": 0.0,
            "call_oi": 0.0,
            "oi_wall": {"strike": float("nan"), "oi": float("nan")},
            "option_expiry": None,
            "intent_score": float("nan"),
            "rows": 0,
            "option_chain_unavailable": True,
        }
        return skip, ""
    close_col = "close" if "close" in df.columns else df.columns[-1]
    series = pd.to_numeric(df[close_col], errors="coerce")
    series_non_na = series.dropna()
    close_last = float(series_non_na.iloc[-1]) if not series_non_na.empty else float("nan")
    close_prev = float(series_non_na.iloc[-2]) if len(series_non_na) >= 2 else float("nan")
    ret1d = (
        ((close_last / close_prev) - 1.0) * 100.0
        if (not math.isnan(close_last) and not math.isnan(close_prev) and close_prev != 0.0)
        else float("nan")
    )
    z = calculate_z_score(series, window=20)
    vpr_raw = volume_participation_ratio(df)
    vpr_calibrated_raw, vpr_for_scoring, vpr_calibration = _calibrate_volume_participation_v2(
        cfg,
        inst,
        vpr_raw,
    )
    ema_200 = calculate_ema(series, span=200)
    ema_distance_pct = (
        ((close_last / ema_200) - 1.0) * 100.0
        if (not math.isnan(close_last) and not math.isnan(ema_200) and ema_200 != 0.0)
        else float("nan")
    )
    atr_14 = calculate_atr(df, window=14)
    atr_14_pct = (
        (atr_14 / close_last) * 100.0
        if (not math.isnan(atr_14) and not math.isnan(close_last) and close_last != 0.0)
        else float("nan")
    )
    atr_break_multiple = (
        abs(close_last - ema_200) / atr_14
        if (
            not math.isnan(close_last)
            and not math.isnan(ema_200)
            and not math.isnan(atr_14)
            and atr_14 > 0.0
        )
        else float("nan")
    )
    pcr = float("nan")
    intent = calculate_equity_technical_score(z, vpr_for_scoring)
    panic_absorption_proxy = (
        not math.isnan(ret1d) and ret1d < 0.0 and not math.isnan(vpr_raw) and vpr_raw >= 1.5
    )
    trap_exit_proxy = (
        not math.isnan(ret1d) and ret1d > 0.0 and not math.isnan(vpr_raw) and vpr_raw <= 0.5
    )
    event_info = _event_flags_for_symbol(inst.symbol, event_snapshot)
    audit: dict[str, Any] = {
        "benchmark": "equity",
        "sector_mode": True,
        "sector": sector_id,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "exchange_used": exchange_used or inst.exchange,
        "exchange_fallback_used": fallback_used,
        "z_score": z,
        "volume_participation_ratio": vpr_raw,
        "volume_participation_calibrated_ratio": vpr_calibrated_raw,
        "volume_participation_for_scoring": vpr_for_scoring,
        "volume_participation_calibration": vpr_calibration,
        "absorption_ratio": vpr_raw,
        "absorption_calibrated_ratio": vpr_calibrated_raw,
        "absorption_for_scoring": vpr_for_scoring,
        "absorption_calibration": vpr_calibration,
        "close_last": close_last,
        "return_1d_pct": ret1d,
        "ema_200": ema_200,
        "ema_200_distance_pct": ema_distance_pct,
        "atr_14": atr_14,
        "atr_14_pct": atr_14_pct,
        "atr_break_multiple": atr_break_multiple,
        "structural_break_proxy": (
            not math.isnan(atr_break_multiple) and atr_break_multiple >= 1.5
        ),
        "panic_absorption_proxy": panic_absorption_proxy,
        "trap_exit_proxy": trap_exit_proxy,
        **event_info,
        "history_lt_200_sessions": len(series_non_na) < 200,
        "pcr": pcr,
        "put_oi": 0.0,
        "call_oi": 0.0,
        "oi_wall": {"strike": float("nan"), "oi": float("nan")},
        "option_expiry": None,
        "intent_score": intent,
        "effective_intent_score": intent,
        "equity_technical_score": intent,
        "rows": len(df),
        "option_chain_unavailable": True,
        "institutional_flow": {
            "available": False,
            "source": None,
            "note": (
                "FII/DII (and true delivery 'absorption') are not in Breeze daily cash bars. "
                "Wire NSE/BSE institutional + delivery feeds by symbol/date to populate this object. "
                "Next implementation step when you pick a source: add columns or a small table "
                "(e.g. fii_net_crs, dii_net_crs, as_of), ingest in a script, and in build_equity_live_audit "
                "set institutional_flow['available'] = True and fold that into a separate score block "
                "so it is never confused with VPR (volume_participation_ratio)."
            ),
        },
    }
    fundamental = _assess_fundamental_strength(cfg, inst)
    audit["fundamental_status"] = fundamental.get("status", "unavailable")
    audit["fundamental_score"] = fundamental.get("score")
    audit["fundamental_reasons"] = fundamental.get("reasons", [])
    _refresh_symbol_scoring_outputs(audit)
    if not with_narrative:
        return audit, ""
    from brain import generate_titan_narrative

    with _GEMINI_SECTOR_LOCK:
        post = generate_titan_narrative(audit, api_keys=cfg.gemini_api_keys)
    return audit, post


def _event_flags_for_symbol(
    symbol: str, event_snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(event_snapshot, dict):
        return {
            "event_risk_present": False,
            "event_risk_soon": False,
            "event_days_to_next": None,
            "event_types": [],
        }
    events = event_snapshot.get("events")
    if not isinstance(events, list):
        events = []
    sym = "".join(ch for ch in symbol.upper() if ch.isalnum())
    today = datetime.now(IST).date()
    days: list[int] = []
    types: list[str] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        es = "".join(ch for ch in str(raw.get("symbol", "")).upper() if ch.isalnum())
        if es != sym:
            continue
        t = str(raw.get("type", "")).strip().lower()
        if t:
            types.append(t)
        ds = str(raw.get("date", "")).strip()
        try:
            d = datetime.fromisoformat(ds).date()
        except ValueError:
            continue
        days.append((d - today).days)
    if not days:
        return {
            "event_risk_present": False,
            "event_risk_soon": False,
            "event_days_to_next": None,
            "event_types": sorted(set(types)),
        }
    nxt = min(days)
    return {
        "event_risk_present": True,
        "event_risk_soon": nxt <= 3,
        "event_days_to_next": nxt,
        "event_types": sorted(set(types)),
    }


def _process_one(
    cfg: TitanConfig,
    sector_id: str,
    inst: SectorInstrument,
    *,
    event_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from supabase_log import save_audit_log

    breeze = _thread_breeze_session(cfg)
    audit, post = build_equity_live_audit(
        cfg,
        breeze,
        inst,
        sector_id=sector_id,
        strict_data=False,
        event_snapshot=event_snapshot,
    )
    if audit.get("skipped_no_data"):
        logger.warning(
            "Sector instrument skipped (no Breeze data): %s %s",
            inst.symbol,
            inst.exchange,
        )
        return {
            "ok": False,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "post": "",
            "error": f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); skipped",
            "error_code": "no_data_skipped",
        }
    save_audit_log({"audit": audit, "post": post}, cfg)
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "audit": audit,
        "post": post,
        "error": None,
        "error_code": None,
    }


def _process_one_metrics(
    cfg: TitanConfig,
    sector_id: str,
    inst: SectorInstrument,
    *,
    event_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Breeze + metrics only; no Gemini (used with --sector-digest)."""
    breeze = _thread_breeze_session(cfg)
    audit, _ = build_equity_live_audit(
        cfg,
        breeze,
        inst,
        sector_id=sector_id,
        with_narrative=False,
        strict_data=False,
        event_snapshot=event_snapshot,
    )
    if audit.get("skipped_no_data"):
        logger.warning(
            "Sector instrument skipped (no Breeze data): %s %s",
            inst.symbol,
            inst.exchange,
        )
        return {
            "ok": False,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "audit": None,
            "error": f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); skipped",
            "error_code": "no_data_skipped",
        }
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "audit": audit,
        "error": None,
        "error_code": None,
    }


def run_sector_live(
    sector_id: str,
    *,
    max_workers: int | None = None,
    max_symbols: int | None = None,
    digest: bool = True,
    send_email: bool = True,
    macro_snapshot: dict[str, Any] | None = None,
    event_snapshot: dict[str, Any] | None = None,
    instruments_override: list[SectorInstrument] | None = None,
    priority_only: bool = False,
    priority_top_n: int | None = None,
) -> str:
    from email_notify import send_success_post_email
    from breeze_client import create_breeze_session

    cfg = load_config()
    # Preflight Breeze auth once to fail fast on expired tokens.
    # Without this, each worker thread would emit the same auth stacktrace.
    create_breeze_session(cfg)
    if instruments_override is not None:
        instruments = instruments_override
    elif priority_only:
        from sector_priority import load_priority_instruments

        instruments = load_priority_instruments(
            cfg,
            sector_key=sector_id,
            top_n=priority_top_n,
        )
        if not instruments:
            logger.warning(
                "No persisted priority list found for sector=%s (top_n=%s); falling back to full sector list.",
                sector_id,
                priority_top_n,
            )
            instruments = load_sector_instruments(sector_id)
    else:
        instruments = load_sector_instruments(sector_id)
    if not instruments:
        raise RuntimeError(f"[Sector] No instruments loaded for sector {sector_id!r}")

    if max_symbols is not None:
        instruments = instruments[: max(0, int(max_symbols))]

    workers = max_workers if max_workers is not None else MAX_WORKERS
    workers = max(1, min(int(workers), 16))

    results: list[dict[str, Any]] = []
    worker = _process_one_metrics if digest else _process_one
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(worker, cfg, sector_id, inst, event_snapshot=event_snapshot): inst
            for inst in instruments
        }
        for fut in as_completed(future_map):
            inst = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Sector instrument failed: %s %s", inst.symbol, inst.exchange)
                err_row: dict[str, Any] = {
                    "ok": False,
                    "symbol": inst.symbol,
                    "exchange": inst.exchange,
                    "error": str(e),
                    "error_code": _classify_error_code(str(e)),
                }
                if digest:
                    err_row["audit"] = None
                else:
                    err_row["post"] = ""
                results.append(err_row)

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        raise RuntimeError(
            f"[Sector] All {len(results)} instruments failed for sector {sector_id!r}"
        )

    if digest:
        from analysis_store import persist_sector_run_analytics
        from analysis_store import (
            build_comparison_payload,
            persist_llm_digest_memory,
            quality_checks_for_run,
            update_sector_period_rollups,
        )
        from brain import generate_sector_digest_narrative
        from supabase_log import save_audit_log

        ok_results = [r for r in results if r.get("ok")]
        audits = [r["audit"] for r in ok_results]
        quality_gate = _prediction_quality_gate(ok_results, total_count=len(results))
        red_ratio, cluster_downgrades = _apply_cluster_guardrails(ok_results)
        event_adjustments = _apply_event_guardrails(ok_results)
        macro_applied, macro_reason = _apply_macro_guardrails(ok_results, macro_snapshot)
        for r in ok_results:
            _refresh_symbol_scoring_outputs(r["audit"])
        persist_meta = persist_sector_run_analytics(
            cfg,
            sector=sector_id,
            audits=audits,
            mode="sector_digest",
            ok_count=ok_count,
            total_count=len(results),
        )
        update_sector_period_rollups(cfg, sector=sector_id)
        comparison = build_comparison_payload(cfg, sector=sector_id)
        qc_warnings = quality_checks_for_run(audits, comparison=comparison)
        with _GEMINI_SECTOR_LOCK:
            post = generate_sector_digest_narrative(
                audits,
                sector_id=sector_id,
                comparison_context=comparison if comparison.get("enabled") else None,
                api_keys=cfg.gemini_api_keys,
            )
        for r in ok_results:
            save_audit_log({"audit": r["audit"], "post": post}, cfg)

        by_bucket: dict[str, list[dict[str, Any]]] = {
            "high-conviction-momentum": [],
            "constructive-watchlist": [],
            "neutral-weak": [],
            "trap-risk": [],
        }
        for r in ok_results:
            by_bucket[_bucket_name(r["audit"])].append(r)
        failed_rows = [x for x in results if not x.get("ok")]
        skipped_rows = [x for x in failed_rows if _is_skipped_no_data_error(x.get("error"))]
        hard_failed_rows = [x for x in failed_rows if not _is_skipped_no_data_error(x.get("error"))]
        hard_failure_breakdown: dict[str, int] = {}
        for r in hard_failed_rows:
            code = str(r.get("error_code") or _classify_error_code(r.get("error")))
            hard_failure_breakdown[code] = hard_failure_breakdown.get(code, 0) + 1

        today = comparison.get("today") if isinstance(comparison, dict) else {}
        dlt = comparison.get("delta") if isinstance(comparison, dict) else {}
        leaders = comparison.get("leaders", []) if isinstance(comparison, dict) else []
        laggards = comparison.get("laggards", []) if isinstance(comparison, dict) else []

        lines = [
            f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded "
            f"(digest mode: 1 Gemini call)\n",
            "",
            "--- Executive snapshot ---",
            (
                "Deployment mode: ACTIONABLE"
                if quality_gate["passed"]
                else "Deployment mode: WATCHLIST ONLY (quality gate failed)"
            ),
            f"Regime: {(comparison.get('regime') if isinstance(comparison, dict) else 'n/a')}",
            f"Avg effective intent: {_fmt_metric(today.get('avg_effective_intent_score') if isinstance(today, dict) else None)} "
            f"(vs 7d {_fmt_metric(dlt.get('avg_effective_intent_vs_7d') if isinstance(dlt, dict) else None)}, "
            f"vs 30d {_fmt_metric(dlt.get('avg_effective_intent_vs_30d') if isinstance(dlt, dict) else None)})",
            f"Breadth above EMA200: {_fmt_metric(today.get('breadth_above_ema200_pct') if isinstance(today, dict) else None)}%",
            f"Volume participation breadth (VPR>1): {_fmt_metric(today.get('pct_absorption_gt_1') if isinstance(today, dict) else None)}%",
            "",
            "--- Movement summary ---",
        ]
        if leaders:
            lines.append(
                "Leaders: "
                + "; ".join(
                    f"{x.get('symbol')}({_fmt_metric(x.get('effective_intent_score'))})"
                    for x in leaders[:5]
                )
            )
        if laggards:
            lines.append(
                "Laggards: "
                + "; ".join(
                    f"{x.get('symbol')}({_fmt_metric(x.get('effective_intent_score'))})"
                    for x in laggards[:5]
                )
            )
        lines.extend(
            [
                "",
                "--- Buckets ---",
                f"High-conviction momentum: {len(by_bucket['high-conviction-momentum'])}",
                f"Constructive watchlist: {len(by_bucket['constructive-watchlist'])}",
                f"Neutral/weak: {len(by_bucket['neutral-weak'])}",
                f"Trap-risk: {len(by_bucket['trap-risk'])}",
                "",
                "--- LLM forensic narrative ---",
            ]
        )
        lines.extend(
            [
            post.strip(),
            "",
            "--- Risk overlays ---",
            f"Cluster breadth red ratio (<= -1% day): {_fmt_metric(red_ratio * 100.0)}%",
            f"Cluster bullish downgrades applied: {cluster_downgrades}",
            f"Event-risk adjustments applied: {event_adjustments}",
            f"Macro guardrail applied: {'yes' if macro_applied else 'no'} ({macro_reason})",
            (
                "Quality checks: "
                + (", ".join(qc_warnings) if qc_warnings else "ok")
            ),
                "",
                "--- Data reconciliation ---",
                f"Symbols requested: {len(results)} | successful: {ok_count} | skipped(no data): {len(skipped_rows)} | hard failures: {len(hard_failed_rows)}",
                (
                    "Skipped samples: "
                    + (", ".join(f"{x['symbol']}({x['exchange']})" for x in skipped_rows[:8]) if skipped_rows else "none")
                ),
                (
                    "Hard-failure breakdown: "
                    + (", ".join(f"{k}={v}" for k, v in sorted(hard_failure_breakdown.items())) if hard_failure_breakdown else "none")
                ),
                "--- Prediction quality gate ---",
                f"Gate status: {'PASS' if quality_gate['passed'] else 'FAIL'}",
                f"Successful symbols: {quality_gate['ok_count']}/{quality_gate['total_count']} ({_fmt_metric(quality_gate['coverage_ratio'] * 100.0)}%)",
                f"Scored symbols coverage: {_fmt_metric(quality_gate['scored_ratio'] * 100.0)}%",
                f"Top-5 nextWeek mean: {_fmt_metric(quality_gate['top5_next_week_mean'])}",
                f"Signal spread (top-bottom nextWeek): {_fmt_metric(quality_gate['spread_top_bottom'])}",
                (
                    "Gate reasons: none"
                    if quality_gate["passed"]
                    else "Gate reasons: " + "; ".join(quality_gate["reasons"])
                ),
            "",
            "--- Per-symbol metrics ---",
            ]
        )
        # Rank by next-week score first for early winner discovery.
        ranked = sorted(
            ok_results,
            key=lambda x: (
                float("-inf")
                if math.isnan(
                    _safe_float(x["audit"].get("next_week_score", float("nan")))
                )
                else _safe_float(x["audit"].get("next_week_score", float("nan")))
            ),
            reverse=True,
        )
        for i, r in enumerate(ranked):
            lines.append(_format_symbol_metrics_line(r))
            if i < len(ranked) - 1:
                lines.append("")
        for r in sorted(
            (x for x in results if (not x.get("ok")) and not _is_skipped_no_data_error(x.get("error"))),
            key=lambda x: (x["symbol"], x["exchange"]),
        ):
            lines.append("")
            lines.append(f"--- {r['symbol']} ({r['exchange']}) FAILED ---")
            lines.append(r.get("error", "") or "")
        digest_text = "\n".join(lines).strip()
        if persist_meta.get("persisted") and persist_meta.get("run_id"):
            gh_rid = (os.environ.get("GITHUB_RUN_ID") or "").strip() or None
            mem_meta = persist_llm_digest_memory(
                cfg,
                run_id=str(persist_meta["run_id"]),
                sector=sector_id,
                prompt_facts=comparison if comparison.get("enabled") else {"enabled": False},
                output_text=post,
                model_name=None,
                full_digest=digest_text,
                github_run_id=gh_rid,
            )
            if not mem_meta.get("persisted"):
                logger.warning(
                    "llm_digest_memory not saved (sector=%s run_id=%s github_run_id=%s): %s",
                    sector_id,
                    persist_meta.get("run_id"),
                    gh_rid or "",
                    mem_meta,
                )
        if send_email:
            send_success_post_email(digest_text, subject_prefix=f"Titan V12.0 sector {sector_id}")
        print(digest_text)
        return digest_text

    lines = [f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded\n"]
    for r in sorted(results, key=lambda x: (x["symbol"], x["exchange"])):
        if r.get("ok"):
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) ---\n")
            lines.append((r.get("post") or "").strip())
        else:
            if _is_skipped_no_data_error(r.get("error")):
                continue
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) FAILED ---\n{r.get('error', '')}\n")

    digest_out = "\n".join(lines).strip()
    if send_email:
        send_success_post_email(digest_out, subject_prefix=f"Titan V12.0 sector {sector_id}")
    try:
        from analysis_store import persist_sector_run_analytics, update_sector_period_rollups

        ok_audits = [r["audit"] for r in results if r.get("ok") and isinstance(r.get("audit"), dict)]
        persist_sector_run_analytics(
            cfg,
            sector=sector_id,
            audits=ok_audits,
            mode="sector_per_symbol_narrative",
            ok_count=ok_count,
            total_count=len(results),
        )
        update_sector_period_rollups(cfg, sector=sector_id)
    except Exception:
        logger.exception("Analysis store persist hook failed")
    print(digest_out)
    return digest_out
