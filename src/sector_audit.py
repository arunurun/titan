"""Sector-wide equity audits: Supabase universe (CSV fallback), parallel Breeze fetches, digest email."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig, load_config
from sector_registry import SectorInstrument, load_sector_instruments
from tape_metrics import (
    benchmark_relative_returns,
    median_notional_inr_20d,
    percentile_rank_0_100,
    pct_return_n_sessions_back,
)

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
_SECTOR_HEARTBEAT_SECONDS_DEFAULT = 30.0
_SECTOR_NO_PROGRESS_TIMEOUT_SECONDS_DEFAULT = 180.0


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


def _horizon_score_label(score: Any) -> str:
    """
    Labels for next_day / next_week heuristic scores (baseline 50 in _predictive_scores).

    Intentionally aligned with internal gates: >=55 reads as meaningfully constructive,
    not merely >50 vs the mathematical midpoint.
    """
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 70.0:
        return "strong constructive"
    if v >= 55.0:
        return "moderate constructive"
    if v >= 45.0:
        return "neutral band"
    if v >= 35.0:
        return "soft caution"
    return "defensive tilt"


def _horizon_score_bands_text() -> str:
    return "bands: >=70 strong, 55-69 constructive, 45-54 neutral, 35-44 caution, <35 defensive"


def _metric_icon(v: Any, *, bullish_above: float, bearish_below: float) -> str:
    f = _safe_float(v)
    if math.isnan(f):
        return "🟡➡"
    if f >= bullish_above:
        return "🟢⬆"
    if f <= bearish_below:
        return "🔴⬇"
    return "🟡➡"


def _breakout_state_icon(
    pct_to_high: Any,
    pct_above_low: Any,
) -> str:
    to_high = _safe_float(pct_to_high)
    above_low = _safe_float(pct_above_low)
    if math.isnan(to_high) and math.isnan(above_low):
        return "🟡➡"
    if not math.isnan(to_high) and to_high >= 0.0:
        return "🟢⬆"
    if not math.isnan(to_high) and to_high >= -1.0:
        return "🟢⬆"
    if not math.isnan(above_low) and above_low <= 1.0:
        return "🔴⬇"
    return "🟡➡"


def _atr_regime_icon(ratio: Any) -> str:
    v = _safe_float(ratio)
    if math.isnan(v):
        return "🟡➡"
    if v <= 0.9:
        return "🟢⬆"
    if v >= 1.1:
        return "🔴⬇"
    return "🟡➡"


def _state_for_metric(v: Any, *, bullish_above: float, bearish_below: float) -> int:
    f = _safe_float(v)
    if math.isnan(f):
        return 0
    if f >= bullish_above:
        return 1
    if f <= bearish_below:
        return -1
    return 0


def _tape_snapshot_icon(audit: dict[str, Any]) -> str:
    states: list[int] = []
    states.append(_state_for_metric(audit.get("return_1d_pct"), bullish_above=1.0, bearish_below=-1.0))
    states.append(_state_for_metric(audit.get("z_score"), bullish_above=1.0, bearish_below=-1.0))
    states.append(
        _state_for_metric(audit.get("ema_200_distance_pct"), bullish_above=5.0, bearish_below=-5.0)
    )
    states.append(
        _state_for_metric(
            _volume_participation_for_digest_label(audit),
            bullish_above=1.5,
            bearish_below=0.7,
        )
    )
    atr_pct = _safe_float(audit.get("atr_14_pct"))
    if not math.isnan(atr_pct):
        if atr_pct < 2.0:
            states.append(1)
        elif atr_pct > 4.0:
            states.append(-1)
        else:
            states.append(0)
    states.append(_state_for_metric(audit.get("cmf_20"), bullish_above=0.05, bearish_below=-0.05))
    if not states:
        return "🟡➡"
    avg = sum(states) / len(states)
    if avg >= 0.25:
        return "🟢⬆"
    if avg <= -0.25:
        return "🔴⬇"
    return "🟡➡"


def _sector_relative_rank_icon(audit: dict[str, Any]) -> str:
    states = [
        _state_for_metric(
            audit.get("sector_pctile_effective_intent"),
            bullish_above=67.0,
            bearish_below=33.0,
        ),
        _state_for_metric(
            audit.get("sector_pctile_next_week_score"),
            bullish_above=67.0,
            bearish_below=33.0,
        ),
    ]
    avg = sum(states) / len(states)
    if avg >= 0.5:
        return "🟢⬆"
    if avg <= -0.5:
        return "🔴⬇"
    return "🟡➡"


def _adx_strength_band(adx_val: Any) -> str:
    adx = _safe_float(adx_val)
    if math.isnan(adx):
        return "n/a"
    if adx >= 25.0:
        return "strong (>=25)"
    if adx >= 20.0:
        return "building (20-25)"
    return "weak (<20)"


def _trend_regime_label(adx_val: Any, plus_di_val: Any, minus_di_val: Any) -> str:
    adx = _safe_float(adx_val)
    plus_di = _safe_float(plus_di_val)
    minus_di = _safe_float(minus_di_val)
    if math.isnan(adx):
        return "Sideways"
    if adx < 20.0:
        return "Sideways"
    if not math.isnan(plus_di) and not math.isnan(minus_di):
        if plus_di > minus_di:
            return "Buy trend"
        if minus_di > plus_di:
            return "Sell trend"
    return "Sideways"


def _atr_ratio_band(atr_ratio: Any) -> str:
    ratio = _safe_float(atr_ratio)
    if math.isnan(ratio):
        return "n/a"
    if ratio < 0.9:
        return "low"
    if ratio <= 1.1:
        return "normal"
    return "high"


def _cmf_band(cmf_val: Any) -> str:
    cmf = _safe_float(cmf_val)
    if math.isnan(cmf):
        return "n/a"
    if cmf > 0.05:
        return "accumulation"
    if cmf < -0.05:
        return "distribution"
    return "neutral"


def _sector_rank_band(percentile: Any) -> str:
    p = _safe_float(percentile)
    if math.isnan(p):
        return "n/a"
    if p >= 67.0:
        return "leader"
    if p <= 33.0:
        return "laggard"
    return "average"


def _range_position_context(pct_to_high: Any, pct_above_low: Any) -> str:
    to_high = _safe_float(pct_to_high)
    above_low = _safe_float(pct_above_low)
    if math.isnan(to_high) and math.isnan(above_low):
        return "range context unavailable"
    if not math.isnan(to_high) and to_high >= -1.0:
        return "near-high (within ~1% of 20D high)"
    if not math.isnan(above_low) and above_low <= 1.0:
        return "near-low (within ~1% of 20D low)"
    return "inside 20D range"


def _cmf_or_obv_for_digest(audit: dict[str, Any]) -> tuple[str, float]:
    cmf = _safe_float(audit.get("cmf_20"))
    if not math.isnan(cmf):
        return "CMF20", cmf
    obv_slope = _safe_float(audit.get("obv_slope_20"))
    if not math.isnan(obv_slope):
        return "OBV slope", obv_slope
    return "CMF20", float("nan")


def _high_volume_down_day_stress(audit: dict[str, Any]) -> bool:
    """Elevated turnover on a negative session (not order-flow absorption)."""
    return bool(audit.get("high_volume_down_day_proxy") or audit.get("panic_absorption_proxy"))


def _digest_verbose_symbol_lines_enabled() -> bool:
    """Long single-line payload for power users / debugging (default: short blocks)."""
    return (os.environ.get("TITAN_DIGEST_VERBOSE_SYMBOLS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _digest_verbose_sections_enabled() -> bool:
    """Enable long top digest sections for deep inspection."""
    return (os.environ.get("TITAN_DIGEST_VERBOSE_SECTIONS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _digest_report_only_mode_enabled() -> bool:
    return (os.environ.get("TITAN_RECONCILE_REPORT_ONLY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _digest_reconcile_mode_enabled() -> bool:
    return (os.environ.get("TITAN_RECONCILE_MODE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _sell_signal_plain_english(signal: str) -> str:
    from action_signals import action_signal_plain_english

    return action_signal_plain_english(signal)


def _digest_flags_simple(audit: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if _high_volume_down_day_stress(audit):
        out.append("heavy turnover on a down day (volume stress; not delivery/absorption)")
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
        ("tapeBlend", _safe_float(week.get("tech_composite_term"))),
        ("trend", _safe_float(week.get("ema_term"))),
        ("momentum1d", _safe_float(week.get("ret1d_term"))),
        ("momentum5d", _safe_float(week.get("ret5d_term"))),
        ("vsNifty5d", _safe_float(week.get("rel5_term"))),
        ("volatility", -_safe_float(week.get("atr_penalty"))),
    ]
    drivers = [name for name, val in contributors if not math.isnan(val) and val >= 1.0]
    drags = [name for name, val in contributors if not math.isnan(val) and val <= -1.0]
    atr_pen = _safe_float(week.get("atr_penalty"))
    if not math.isnan(atr_pen) and atr_pen >= 1.0:
        drags.append("volatility")
    drv = drivers[0] if drivers else None
    drag = drags[0] if drags else None

    parts = [
        "Model read confidence: "
        f"{confidence} (bands: >=70 high, 55-69 medium, <55 low; directional heuristic, not a guarantee)"
    ]
    if drv:
        parts.append(f"{drv} supportive")
    if drag:
        parts.append(f"{drag} weighing on the score")
    if penalties:
        parts.append(f"flags: {'; '.join(str(p) for p in penalties[:2])}")
    return " · ".join(parts)


def _infer_news_affected_metric(audit: dict[str, Any]) -> str:
    breakdown = audit.get("prediction_breakdown")
    if not isinstance(breakdown, dict):
        return "technical intent"
    week = breakdown.get("week", {}) if isinstance(breakdown.get("week"), dict) else {}
    candidates = [
        ("trend", abs(_safe_float(week.get("ema_term")))),
        ("momentum 1D", abs(_safe_float(week.get("ret1d_term")))),
        ("momentum 5D", abs(_safe_float(week.get("ret5d_term")))),
        ("benchmark relative 5D", abs(_safe_float(week.get("rel5_term")))),
        ("volatility", abs(_safe_float(week.get("atr_penalty")))),
        ("tape blend", abs(_safe_float(week.get("tech_composite_term")))),
    ]
    ranked = [(name, v) for name, v in candidates if not math.isnan(v)]
    if not ranked:
        return "technical intent"
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[0][0]


def _news_scope_bucket(*, title: str, source: str) -> str:
    txt = f"{title} {source}".lower()
    stock_terms = (
        "stock",
        "stocks",
        "share",
        "shares",
        "earnings",
        "guidance",
        "ipo",
        "buyback",
        "merger",
        "acquisition",
        "quarter",
        "q1",
        "q2",
        "q3",
        "q4",
    )
    local_terms = (
        "india",
        "indian",
        "nse",
        "bse",
        "nifty",
        "sensex",
        "rbi",
        "sebi",
        "rupee",
        "et now",
        "moneycontrol",
        "livemint",
        "economictimes",
        "business standard",
    )
    if any(term in txt for term in stock_terms):
        return "stock"
    if any(term in txt for term in local_terms):
        return "local"
    return "global"


def _news_evidence_payload(
    *,
    drivers: list[dict[str, Any]],
    net_score: float,
    max_per_bucket: int = 3,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {"global": [], "local": [], "stock": []}
    for raw in drivers:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("driver") or "").strip()
        source = str(raw.get("source") or "").strip() or "unknown_source"
        published_at = str(raw.get("published_at") or "").strip()
        contribution = _safe_float(raw.get("contribution"))
        if not title or math.isnan(contribution):
            continue
        scope = _news_scope_bucket(title=title, source=source)
        buckets.setdefault(scope, []).append(
            {
                "headline": title,
                "source": source,
                "published_at": published_at,
                "impact_contribution_score": round(contribution, 4),
            }
        )
    for scope in ("global", "local", "stock"):
        buckets[scope] = sorted(
            buckets.get(scope, []),
            key=lambda x: abs(_safe_float(x.get("impact_contribution_score"))),
            reverse=True,
        )[: max(1, int(max_per_bucket))]
    if net_score > 0.02:
        direction = "tailwind"
    elif net_score < -0.02:
        direction = "headwind"
    else:
        direction = "neutral"
    return {
        "top_headlines": buckets,
        "net_news_impact_score": round(net_score, 4),
        "net_news_impact_direction": direction,
    }


def _stock_news_fetch_reason(corr: dict[str, Any], evidence: dict[str, Any]) -> str:
    explicit = str(evidence.get("stock_fetch_error") or "").strip()
    if explicit:
        return explicit
    stock_meta = corr.get("stock_news")
    if isinstance(stock_meta, dict):
        return str(stock_meta.get("fetch_error") or "").strip()
    return ""


def _news_evidence_line(audit: dict[str, Any]) -> str:
    corr = audit.get("news_correlation")
    if not isinstance(corr, dict):
        return ""
    evidence = corr.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    net_score = _safe_float(evidence.get("net_news_impact_score"))
    direction = str(evidence.get("net_news_impact_direction") or "neutral").strip().lower() or "neutral"
    top_headlines = evidence.get("top_headlines")
    if not isinstance(top_headlines, dict):
        top_headlines = {}
    stock_fetch_error = _stock_news_fetch_reason(corr, evidence)
    stock_query_used = ""
    stock_meta = corr.get("stock_news")
    if isinstance(stock_meta, dict):
        stock_query_used = str(stock_meta.get("query_used") or "").strip()

    def _bucket_txt(name: str) -> str:
        rows = top_headlines.get(name)
        if not isinstance(rows, list) or not rows:
            if name == "stock":
                if stock_fetch_error:
                    detail = f"fetch_error={stock_fetch_error}"
                    if stock_query_used:
                        detail += f"; query={stock_query_used}"
                    return f"stock=none ({detail})"
                coverage = str(corr.get("stock_news_coverage") or "").strip()
                if coverage == "helper_unavailable":
                    return "stock=none (fetch_error=helper_unavailable)"
            return f"{name}=none"
        parts: list[str] = []
        for row in rows[:2]:
            if not isinstance(row, dict):
                continue
            headline = str(row.get("headline") or "").strip()
            if not headline:
                continue
            source = str(row.get("source") or "unknown_source").strip()
            published_at = str(row.get("published_at") or "n/a").strip() or "n/a"
            impact = _fmt_metric(row.get("impact_contribution_score"), 4)
            parts.append(
                f"{headline} [source={source}; published_at={published_at}; impact_contribution_score={impact}]"
            )
        return f"{name}=" + (" || ".join(parts) if parts else "none")

    return (
        f"News evidence: net_news_impact_score={_fmt_metric(net_score, 4)} "
        f"(direction={direction}) · {_bucket_txt('global')} · {_bucket_txt('local')} · "
        f"{_bucket_txt('market')} · {_bucket_txt('stock')}"
    )


def _news_correlation_line(audit: dict[str, Any]) -> str:
    corr = audit.get("news_correlation")
    if not isinstance(corr, dict):
        return "News correlation unavailable: correlation metadata missing"
    unavailable_reason = str(corr.get("unavailable_reason") or "").strip()
    if unavailable_reason:
        return f"News correlation unavailable: {unavailable_reason}"
    if corr.get("available") is False:
        return "News correlation unavailable: correlation data unavailable"
    driver = str(corr.get("driver") or "").strip()
    metric = str(corr.get("affected_metric") or "").strip()
    theme = str(corr.get("affected_theme") or "").strip()
    direction = str(corr.get("direction") or "").strip().lower()
    conf = _safe_float(corr.get("confidence"))
    driver_source = str(corr.get("driver_source") or "").strip().lower() or "macro"
    stock_news_fetched_raw = _safe_float(corr.get("stock_news_fetched_count"))
    stock_news_fetched_count = 0 if math.isnan(stock_news_fetched_raw) else int(stock_news_fetched_raw)
    coverage_status = str(corr.get("stock_news_coverage") or "").strip().lower() or "unknown"
    fallback_label = str(corr.get("fallback_label") or "").strip()
    if not driver or not metric:
        return "News correlation unavailable: incomplete correlation payload"
    if direction == "tailwind":
        dir_label = "tailwind"
    elif direction == "headwind":
        dir_label = "headwind"
    else:
        dir_label = "neutral"
    conf_txt = _fmt_metric(conf) if not math.isnan(conf) else "n/a"
    if math.isnan(conf):
        conf_band = "n/a"
    elif conf >= 0.75:
        conf_band = "high"
    elif conf >= 0.50:
        conf_band = "medium"
    else:
        conf_band = "low"
    theme_txt = theme if theme else "global macro"
    if driver_source == "stock":
        return (
            f"Stock news relation: stock_driver={driver} · theme={theme_txt} · "
            f"affected_metric={metric} · direction={dir_label} · confidence={conf_txt} "
            f"({conf_band}; bands: >=0.75 high, 0.50-0.74 medium, <0.50 low) · "
            f"stock_news_fetched_count={stock_news_fetched_count} · coverage={coverage_status}"
        )
    fallback_reason = "stock_headlines_missing_using_macro_context"
    if coverage_status == "not_covered":
        fallback_reason = "stock_news_not_fetched_for_symbol_using_macro_context"
    elif coverage_status == "helper_unavailable":
        fallback_reason = "stock_news_helper_unavailable_using_macro_context"
    elif coverage_status.startswith("empty:"):
        fallback_reason = f"stock_news_{coverage_status[6:]}_using_macro_context"
    elif fallback_label:
        fallback_reason = fallback_label.replace("sector_specific_match_missing_", "")
    stock_meta = corr.get("stock_news")
    fetch_error = ""
    if isinstance(stock_meta, dict):
        fetch_error = str(stock_meta.get("fetch_error") or "").strip()
    fetch_error_txt = f" · stock_fetch_error={fetch_error}" if fetch_error else ""
    return (
        f"Macro fallback relation: macro_driver={driver} · theme={theme_txt} · "
        f"affected_metric={metric} · direction={dir_label} · confidence={conf_txt} "
        f"({conf_band}; bands: >=0.75 high, 0.50-0.74 medium, <0.50 low) · "
        f"fallback_reason={fallback_reason} · stock_news_fetched_count={stock_news_fetched_count} "
        f"· coverage={coverage_status}{fetch_error_txt}"
        + (f" · fallback={fallback_label}" if fallback_label else "")
    )


def _cmf_delta_interpretation(absolute_change: float, current_value: float) -> str:
    if absolute_change >= 0.03:
        return "strong_increase"
    if absolute_change > 0.005:
        return "increase"
    if absolute_change <= -0.03:
        return "strong_decrease"
    if absolute_change < -0.005:
        return "decrease"
    if current_value > 0.05:
        return "stable_accumulation"
    if current_value < -0.05:
        return "stable_distribution"
    return "stable_neutral"


def _cmf_delta_payload(previous_value: Any, current_value: Any) -> dict[str, Any]:
    prev = _safe_float(previous_value)
    cur = _safe_float(current_value)
    if math.isnan(prev) or math.isnan(cur):
        return {
            "previous_value": None,
            "current_value": None if math.isnan(cur) else round(cur, 4),
            "absolute_change": None,
            "relative_change_percent": None,
            "interpretation": "unavailable",
        }
    abs_change = cur - prev
    rel_pct = None
    if abs(prev) > 1e-9:
        rel_pct = (abs_change / abs(prev)) * 100.0
    return {
        "previous_value": round(prev, 4),
        "current_value": round(cur, 4),
        "absolute_change": round(abs_change, 4),
        "relative_change_percent": round(rel_pct, 2) if isinstance(rel_pct, float) else None,
        "interpretation": _cmf_delta_interpretation(abs_change, cur),
    }


def _cmf_delta_line(audit: dict[str, Any]) -> str:
    payload = audit.get("cmf_20_delta")
    if not isinstance(payload, dict):
        return ""
    prev = payload.get("previous_value")
    cur = payload.get("current_value")
    abs_change = payload.get("absolute_change")
    rel = payload.get("relative_change_percent")
    interpretation = str(payload.get("interpretation") or "unavailable")
    if prev is None or cur is None or abs_change is None:
        return "CMF20 delta: unavailable"
    rel_txt = "n/a" if rel is None else f"{float(rel):+.2f}%"
    return (
        f"CMF20 delta: previous_value={_fmt_metric(prev, 4)} -> current_value={_fmt_metric(cur, 4)} "
        f"| absolute_change={_fmt_metric(abs_change, 4)} | relative_change_percent={rel_txt} "
        f"| interpretation={interpretation}"
    )


def _format_symbol_metrics_line_simple(result: dict[str, Any]) -> str:
    symbol = result["symbol"]
    exchange = result["exchange"]
    audit = result["audit"]
    z = audit.get("z_score")
    intent = audit.get("effective_intent_score", audit.get("intent_score"))
    vpr_label_src = _volume_participation_for_digest_label(audit)
    ret1d = audit.get("return_1d_pct")
    atr_pct = audit.get("atr_14_pct")
    adx_14 = audit.get("adx_14")
    breakout_to_high = audit.get("breakout_20d_distance_pct_to_high")
    breakout_above_low = audit.get("breakout_20d_distance_pct_above_low")
    atr_ratio = audit.get("atr_14_over_atr_63")
    plus_di_14 = audit.get("adx_plus_di_14")
    minus_di_14 = audit.get("adx_minus_di_14")
    cmf_label, cmf_val = _cmf_or_obv_for_digest(audit)
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
    nw_l = _horizon_score_label(next_week)
    lines_out.append(
        f"{_metric_icon(next_week, bullish_above=55.0, bearish_below=45.0)} "
        f"1W outlook: {_fmt_metric(next_week)} / 100 ({nw_l}; {_horizon_score_bands_text()})"
    )
    lines_out.append(
        f"{_metric_icon(intent, bullish_above=55.0, bearish_below=45.0)} "
        f"Technical intent: {_fmt_metric(intent)} / 100 "
        f"({_equity_technical_label(intent)}; bands: >=70 high-long, 55-69 moderate-long, 45-54 neutral, 30-44 defensive, <30 high-defensive)"
    )
    lines_out.append("")
    trend_regime = _trend_regime_label(adx_14, plus_di_14, minus_di_14)
    if not math.isnan(_safe_float(plus_di_14)) and not math.isnan(_safe_float(minus_di_14)):
        if _safe_float(plus_di_14) > _safe_float(minus_di_14):
            direction_rule = (
                f"+DI {_fmt_metric(plus_di_14)} > -DI {_fmt_metric(minus_di_14)} => buy trend"
            )
        elif _safe_float(plus_di_14) < _safe_float(minus_di_14):
            direction_rule = (
                f"+DI {_fmt_metric(plus_di_14)} < -DI {_fmt_metric(minus_di_14)} => sell trend"
            )
        else:
            direction_rule = (
                f"+DI {_fmt_metric(plus_di_14)} = -DI {_fmt_metric(minus_di_14)} => neutral trend"
            )
    else:
        direction_rule = "direction source unavailable"
    trend_icon = "🟡➡"
    if trend_regime == "Buy trend":
        trend_icon = "🟢⬆"
    elif trend_regime == "Sell trend":
        trend_icon = "🔴⬇"
    lines_out.append(
        f"{trend_icon} Trend regime (14D): {trend_regime} "
        f"(ADX {_fmt_metric(adx_14)}; strength {_adx_strength_band(adx_14)}; "
        f"strength bands: <20 sideways, 20-24 weak trend, >=25 strong trend; direction rule: {direction_rule})"
    )
    lines_out.append("")
    lines_out.append(
        f"{_breakout_state_icon(breakout_to_high, breakout_above_low)} "
        f"20D Range Position: {_fmt_metric(breakout_to_high)}% to 20D high \u00b7 "
        f"{_fmt_metric(breakout_above_low)}% above 20D low "
        "(near-high (within ~1% of 20D high); thresholds: near-high >=-1%, near-low <=1%)"
    )
    lines_out.append("")
    lines_out.append(
        f"{_atr_regime_icon(atr_ratio)} Volatility vs 3M baseline: {_fmt_metric(atr_ratio)}x "
        f"({_atr_ratio_band(atr_ratio)}; bands: <0.90 low, 0.90-1.10 normal, >1.10 high)"
    )
    lines_out.append("")
    lines_out.append(f"{_tape_snapshot_icon(audit)} Tape snapshot")
    lines_out.append(
        f"{_metric_icon(ret1d, bullish_above=1.0, bearish_below=-1.0)} "
        f"1D move: {_fmt_metric(ret1d)}% "
        f"(bands: >=+1 strong up, -1 to +1 muted, <=-1 weak)"
    )
    lines_out.append(
        f"{_metric_icon(z, bullish_above=1.0, bearish_below=-1.0)} "
        f"1D z-score: {_fmt_metric(z)} "
        f"({_z_label(z)}; bands: >=+2 / +1 to +2 / -1 to +1 / -2 to -1 / <=-2)"
    )
    lines_out.append(
        f"{_metric_icon(ema_dist, bullish_above=5.0, bearish_below=-5.0)} "
        f"Distance above long-term trend (EMA200): {_fmt_metric(ema_dist)}% "
        f"(bands: >+5 stretched above trend, -5 to +5 near trend, <-5 below trend)"
    )
    lines_out.append(
        f"{_metric_icon(vpr_label_src, bullish_above=1.5, bearish_below=0.7)} "
        f"Volume participation: {_fmt_metric(vpr_label_src)}x "
        f"({_volume_participation_label(vpr_label_src)}; bands: >=1.5 high, 1.0-1.49 above-avg, 0.7-0.99 below-avg, <0.7 thin)"
    )
    atr_icon = "🟡➡"
    atr_v = _safe_float(atr_pct)
    if not math.isnan(atr_v):
        if atr_v < 2.0:
            atr_icon = "🟢⬆"
        elif atr_v > 4.0:
            atr_icon = "🔴⬇"
    lines_out.append(
        f"{atr_icon} Typical daily swing (ATR14): {_fmt_metric(atr_pct)}% "
        f"(bands: <2.0 calm, 2.0-4.0 moderate, >4.0 elevated)"
    )
    lines_out.append(
        f"{_metric_icon(cmf_val, bullish_above=0.05, bearish_below=-0.05)} "
        f"Money flow trend (20D): {_fmt_metric(cmf_val, 3)} "
        f"({_cmf_band(cmf_val)}; bands: >0.05 accumulation, -0.05 to 0.05 neutral, < -0.05 distribution)"
        + (f" [{cmf_label} proxy]" if cmf_label != "CMF20" else "")
    )
    lines_out.append("")

    lines_out.append(f"{_sector_relative_rank_icon(audit)} Sector-relative rank")
    sp_int = audit.get("sector_pctile_effective_intent")
    lines_out.append(
        f"{_metric_icon(sp_int, bullish_above=67.0, bearish_below=33.0)} "
        f"Technical intent percentile: {_fmt_metric(sp_int)} "
        f"({_sector_rank_band(sp_int)}; bands: leader >=67, average 34-66, laggard <=33)"
    )
    sp_nw = audit.get("sector_pctile_next_week_score")
    lines_out.append(
        f"{_metric_icon(sp_nw, bullish_above=67.0, bearish_below=33.0)} "
        f"Next-week percentile: {_fmt_metric(sp_nw)} "
        f"({_sector_rank_band(sp_nw)}; bands: leader >=67, average 34-66, laggard <=33)"
    )
    nd_l = _horizon_score_label(nf)
    lines_out.append(
        f"{_metric_icon(nf, bullish_above=55.0, bearish_below=45.0)} "
        f"Very short horizon (1D outlook): {_fmt_metric(nf)} / 100 "
        f"({nd_l}; {_horizon_score_bands_text()})"
    )

    if sell_reasons:
        sr = "; ".join(str(x) for x in sell_reasons[:3])
        lines_out.append(f"Why this action: {sr}")

    pred = _prediction_brief_line(audit)
    if pred:
        lines_out.append(pred)

    cmf_delta = _cmf_delta_line(audit)
    if cmf_delta:
        lines_out.append(cmf_delta)

    news_rel = _news_correlation_line(audit)
    if news_rel:
        lines_out.append(news_rel)
    news_evidence = _news_evidence_line(audit)
    if news_evidence:
        lines_out.append(news_evidence)

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
    if _high_volume_down_day_stress(audit):
        flags.append("high-vol-down-day")
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
    zb = str(audit.get("z_score_blend") or "n/a")
    base = (
        f"{symbol} ({exchange}) | techIntent {_fmt_metric(intent)} [{_equity_technical_label(intent)}] "
        f"| z-score {_fmt_metric(z)} ({_z_label(z)}; std-dev from mean; {zb}) "
        f"| volPart ratio {_fmt_metric(vpr, 3)}x (vs symbol baseline volume) "
        f"| score-input {_fmt_metric(vpr_score, 2)} [{_volume_participation_label(vpr_score)}; used in techIntent]"
        f"{vpr_cap_suffix} "
        f"| move1D {_fmt_metric(ret1d)}% "
        f"| distVs200DTrend {_fmt_metric(ema_dist)}% | atr14 {_fmt_metric(atr_pct)}% "
        f"| next1D {_fmt_metric(next_day)} ({_horizon_score_label(next_day)}) "
        f"| next1W {_fmt_metric(next_week)} ({_horizon_score_label(next_week)}) "
        f"| sell {sell_signal} "
        f"| flags={flag_text} | rows {rows} "
        f"| exchange_used={exchange_used}{' (fallback)' if fallback_used else ''}"
    )
    sell_reason_text = (
        f" ({'; '.join(str(x) for x in sell_reasons[:2])})" if sell_reasons else ""
    )
    cmf_delta = _cmf_delta_line(audit)
    news_rel = _news_correlation_line(audit)
    news_evidence = _news_evidence_line(audit)
    return (
        f"{base} | support={support_tag} | fundamentals={fundamental_text} "
        f"| sellReason={sell_signal}{sell_reason_text} | {_prediction_reason_text(audit)}"
        + (f" | {cmf_delta}" if cmf_delta else "")
        + (f" | {news_rel}" if news_rel else "")
        + (f" | {news_evidence}" if news_evidence else "")
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


def _volume_participation_for_digest_label(audit: dict[str, Any]) -> Any:
    """
    Prefer the calibrated / capped participation series used for intent scoring so
    digest tape wording matches the equity technical composite.
    """
    for key in (
        "volume_participation_for_scoring",
        "absorption_for_scoring",
        "volume_participation_ratio",
        "absorption_ratio",
    ):
        if key not in audit:
            continue
        v = _safe_float(audit.get(key))
        if not math.isnan(v):
            return audit.get(key)
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
    if "historical fetch timeout" in msg:
        return "data_fetch_timeout"
    if "no-progress watchdog timeout" in msg:
        return "sector_no_progress_watchdog"
    if "historical fetch failed after retries" in msg:
        return "data_fetch_failed"
    if "session token expired" in msg or "auth/permission" in msg:
        return "auth_or_session"
    return "runtime_error"


def _sector_heartbeat_seconds() -> float:
    raw = (os.environ.get("TITAN_SECTOR_HEARTBEAT_SECONDS") or "").strip()
    if not raw:
        return _SECTOR_HEARTBEAT_SECONDS_DEFAULT
    try:
        return max(5.0, float(raw))
    except ValueError:
        return _SECTOR_HEARTBEAT_SECONDS_DEFAULT


def _sector_no_progress_timeout_seconds() -> float:
    raw = (os.environ.get("TITAN_SECTOR_NO_PROGRESS_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return _SECTOR_NO_PROGRESS_TIMEOUT_SECONDS_DEFAULT
    try:
        # Keep sane lower-bound to avoid false positives on normal API jitter.
        return max(20.0, float(raw))
    except ValueError:
        return _SECTOR_NO_PROGRESS_TIMEOUT_SECONDS_DEFAULT


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
    ret5d = _safe_float(audit.get("return_5d_pct"))
    ret10d = _safe_float(audit.get("return_10d_pct"))
    rel5 = _safe_float(audit.get("rel_return_5d_vs_nifty_pct"))
    rel20 = _safe_float(audit.get("rel_return_20d_vs_nifty_pct"))
    ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
    atr_in = _safe_float(audit.get("atr_penalty_input"))
    if math.isnan(atr_in):
        atr_in = _safe_float(audit.get("atr_14_pct"))
    ema_conf = _ema_history_confidence(audit.get("rows"))

    tech_day = 0.0 if math.isnan(tech) else ((tech - 50.0) * 0.52)
    tech_week = 0.0 if math.isnan(tech) else ((tech - 50.0) * 0.62)
    ret1_w = 0.18 if audit.get("extreme_price_move_proxy") else 0.42
    ret_term = 0.0 if math.isnan(ret1d) else (ret1d * ret1_w)
    ret5_term = 0.0 if math.isnan(ret5d) else (ret5d * 0.28)
    ret10_term = 0.0 if math.isnan(ret10d) else (ret10d * 0.15)
    rel5_term = 0.0 if math.isnan(rel5) else (rel5 * 0.2)
    rel20_term = 0.0 if math.isnan(rel20) else (rel20 * 0.11)
    ema_base = 0.0 if math.isnan(ema_dist) else (ema_dist * 0.26 * ema_conf)
    ema_day = ema_base * 0.85
    ema_week = ema_base * 1.0
    atr_penalty = 0.0 if math.isnan(atr_in) else (atr_in * 0.45)

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
    }
    return _clamp_score(day_score), _clamp_score(week_score), breakdown


def _bucket_name(audit: dict[str, Any]) -> str:
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ab = _safe_float(_volume_participation_for_digest_label(audit))
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
    ab = _safe_float(_volume_participation_for_digest_label(audit))
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
        ("tapeBlend", _safe_float(week.get("tech_composite_term"))),
        ("trend", _safe_float(week.get("ema_term"))),
        ("momentum1d", _safe_float(week.get("ret1d_term"))),
        ("momentum5d", _safe_float(week.get("ret5d_term"))),
        ("vsNifty5d", _safe_float(week.get("rel5_term"))),
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
        f"confidence={confidence} (directional heuristic, not certainty) | "
        f"drivers={','.join(drivers[:3])} | drags={','.join(drags[:3])} | penalties={pen} "
        f"| factors day[tech {_fmt_metric(day.get('tech_composite_term'))}, "
        f"ret {_fmt_metric(day.get('ret1d_term'))}, ema {_fmt_metric(day.get('ema_term'))}, "
        f"emaConf {_fmt_metric(day.get('ema_history_confidence'))}, atr-pen {_fmt_metric(day.get('atr_penalty'))}] "
        f"week[tech {_fmt_metric(week.get('tech_composite_term'))}, "
        f"ret {_fmt_metric(week.get('ret1d_term'))}, ema {_fmt_metric(week.get('ema_term'))}, "
        f"emaConf {_fmt_metric(week.get('ema_history_confidence'))}, atr-pen {_fmt_metric(week.get('atr_penalty'))}]"
    )


def _derive_sell_signal(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    from action_signals import derive_action_signal

    return derive_action_signal(audit)


def _refresh_symbol_scoring_outputs(audit: dict[str, Any]) -> None:
    next_day_score, next_week_score, prediction_breakdown = _predictive_scores(audit)
    audit["next_day_score"] = next_day_score
    audit["next_week_score"] = next_week_score
    audit["prediction_breakdown"] = prediction_breakdown
    audit["hypothesis_support"] = _hypothesis_support_tag(audit)
    action_signal, sell_risk_score, sell_reasons = _derive_sell_signal(audit)
    audit["sell_signal"] = action_signal
    audit["action_signal"] = action_signal
    audit["sell_signal_risk_score"] = sell_risk_score
    audit["sell_signal_reasons"] = sell_reasons


def _enrich_audit_with_symbol_news(
    cfg: TitanConfig,
    inst: SectorInstrument,
    audit: dict[str, Any],
) -> None:
    """Non-blocking per-symbol news enrichment; failures set audit['news_error']."""
    try:
        from news_audit import compute_news_sentiment_trend, correlate_news_with_price_move
        from news_sentiment import aggregate_sentiment
        from news_store import get_recent_news_for_symbol
    except ImportError as exc:
        logger.warning("News modules unavailable for %s: %s", inst.symbol, exc)
        audit["news_error"] = f"news_modules_unavailable:{exc}"
        return

    try:
        lookback_hours = int(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", 36))
        driver_limit = int(os.environ.get("TITAN_NEWS_DRIVER_LIMIT", 3))
        recent_news = get_recent_news_for_symbol(
            cfg,
            inst.symbol,
            inst.exchange,
            lookback_hours=lookback_hours,
            limit=driver_limit * 2,
        )
        if recent_news:
            audit["recent_news"] = recent_news[:driver_limit]
            sentiment_agg = aggregate_sentiment(recent_news)
            audit["news_sentiment_aggregate"] = sentiment_agg["aggregate_sentiment"]
            audit["news_sentiment_score"] = sentiment_agg["aggregate_score"]
            audit["news_count"] = len(recent_news)
            trend = compute_news_sentiment_trend(cfg, inst.symbol)
            audit["news_sentiment_trend"] = trend["trend"]
            audit["news_sentiment_trend_score"] = trend["trend_score"]
            corr = correlate_news_with_price_move(cfg, inst.symbol, audit)
            audit["news_price_alignment"] = corr["aligned"]
            if not corr["aligned"]:
                audit["news_price_contradiction"] = corr["contradiction_strength"]
                audit["news_price_contradiction_reason"] = corr["possible_reason"]
        else:
            audit["recent_news"] = []
            audit["news_count"] = 0
            audit["news_sentiment_aggregate"] = "neutral"
            audit["news_sentiment_score"] = 0.0
    except Exception as exc:
        logger.warning("News enrichment failed for %s: %s", inst.symbol, exc)
        audit["news_error"] = str(exc)


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


def _stock_news_coverage_top_n() -> int:
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_COVERAGE_TOP_N", "")) or "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _apply_global_news_correlation(
    cfg: TitanConfig,
    *,
    sector_id: str,
    ok_results: list[dict[str, Any]],
) -> dict[str, Any]:
    resolve_global_news_snapshot = None
    fetch_stock_news_for_symbol = None
    correlate_stock_news_with_macro = None
    helper_unavailable: list[str] = []
    try:
        from sector_priority import resolve_global_news_snapshot as _resolver

        resolve_global_news_snapshot = _resolver
    except Exception as exc:
        helper_unavailable.append("resolve_global_news_snapshot")
        logger.warning("News correlation snapshot resolver unavailable: %s", exc)
    try:
        from sector_priority import fetch_stock_news_for_symbol as _fetcher

        fetch_stock_news_for_symbol = _fetcher
    except Exception as exc:
        helper_unavailable.append("fetch_stock_news_for_symbol")
        logger.warning("News correlation stock-news helper unavailable: %s", exc)
    try:
        from sector_priority import correlate_stock_news_with_macro as _correlator

        correlate_stock_news_with_macro = _correlator
    except Exception as exc:
        helper_unavailable.append("correlate_stock_news_with_macro")
        logger.warning("News correlation correlator helper unavailable: %s", exc)

    snapshot: dict[str, Any] = {}
    snapshot_reason = ""
    if callable(resolve_global_news_snapshot):
        try:
            raw_snapshot = resolve_global_news_snapshot(cfg)
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            if not isinstance(raw_snapshot, dict):
                snapshot_reason = "snapshot_payload_invalid"
                logger.warning(
                    "News correlation snapshot resolver returned non-dict payload: %s",
                    type(raw_snapshot).__name__,
                )
        except Exception as exc:
            snapshot_reason = f"snapshot_resolver_error:{exc}"
            logger.warning("News correlation snapshot resolution failed: %s", exc)
            snapshot = {}
    else:
        snapshot_reason = "snapshot_resolver_unavailable"
    if not snapshot:
        snapshot = {"source": "unavailable", "sector_scores": {}, "reason": snapshot_reason}
    elif snapshot_reason and not str(snapshot.get("reason") or "").strip():
        snapshot["reason"] = snapshot_reason

    snapshot_source = str(snapshot.get("source") or "").strip().lower()
    snapshot_available = snapshot_source not in ("", "unavailable", "na", "n/a", "none")

    if helper_unavailable:
        logger.warning(
            "News correlation using fallback path; unavailable helpers=%s",
            ",".join(helper_unavailable),
        )
    if not ok_results:
        return {
            "applied": False,
            "reason": "no_results",
            "snapshot": snapshot,
            "snapshot_available": snapshot_available,
            "snapshot_reason": str(snapshot.get("reason") or snapshot_reason or "").strip(),
            "applied_count": 0,
            "stock_news_coverage_count": 0,
            "fallback_count": 0,
        }
    sector_key = str(sector_id).strip().lower()
    coverage_pairs: set[tuple[str, str]] = set()
    for row in ok_results:
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        symbol = str(audit.get("symbol") or row.get("symbol") or "").strip().upper()
        exchange = str(audit.get("exchange") or row.get("exchange") or "").strip().upper()
        if symbol and exchange in ("NSE", "BSE"):
            coverage_pairs.add((symbol, exchange))
    stock_news_by_symbol: dict[tuple[str, str], dict[str, Any]] = {}
    if callable(fetch_stock_news_for_symbol):
        for symbol, exchange in coverage_pairs:
            if not symbol or exchange not in ("NSE", "BSE"):
                continue
            try:
                stock_news_by_symbol[(symbol, exchange)] = fetch_stock_news_for_symbol(
                    cfg,
                    symbol=symbol,
                    exchange=exchange,
                )
            except Exception as exc:
                stock_news_by_symbol[(symbol, exchange)] = {
                    "symbol": symbol,
                    "exchange": exchange,
                    "items": [],
                    "query_used": "",
                    "alias_used": "",
                    "fallback_used": False,
                    "error": f"unexpected:{exc}",
                }
    else:
        logger.warning(
            "News correlation stock-level fetch skipped; helper unavailable for %s symbols",
            len(coverage_pairs),
        )

    scores = snapshot.get("sector_scores")
    scores = scores if isinstance(scores, dict) else {}
    sector_news = scores.get(sector_key) if isinstance(scores.get(sector_key), dict) else {}
    sector_score = _safe_float(sector_news.get("score"))
    if math.isnan(sector_score):
        sector_score = 0.0
    sector_conf = _safe_float(sector_news.get("confidence"))
    drivers = sector_news.get("drivers_top")
    drivers = drivers if isinstance(drivers, list) else []
    applied = 0
    fallback_count = 0
    for r in ok_results:
        audit = r.get("audit")
        if not isinstance(audit, dict):
            continue
        symbol = str(audit.get("symbol") or r.get("symbol") or "").strip().upper()
        exchange = str(audit.get("exchange") or r.get("exchange") or "").strip().upper()
        stock_news_meta = stock_news_by_symbol.get((symbol, exchange), {})
        stock_news_items = stock_news_meta.get("items")
        stock_news_items = stock_news_items if isinstance(stock_news_items, list) else []
        stock_fetch_error = str(stock_news_meta.get("error") or "").strip()
        if not callable(fetch_stock_news_for_symbol):
            coverage_status = "helper_unavailable"
        elif (symbol, exchange) not in stock_news_by_symbol:
            coverage_status = "not_covered"
        elif stock_news_items:
            coverage_status = "fetched"
        elif stock_fetch_error:
            coverage_status = f"empty:{stock_fetch_error}"
        else:
            coverage_status = "empty:unknown"
        unavailable_reason = ""
        corr: dict[str, Any] = {}
        if callable(correlate_stock_news_with_macro):
            try:
                corr = correlate_stock_news_with_macro(
                    symbol=symbol,
                    sector_key=sector_key,
                    stock_news_items=stock_news_items,
                    snapshot=snapshot,
                )
                corr = corr if isinstance(corr, dict) else {}
            except Exception as exc:
                logger.warning(
                    "News correlation compute failed for %s (%s): %s; using macro fallback",
                    symbol or "unknown",
                    exchange or "unknown",
                    exc,
                )
                corr = {}
                unavailable_reason = f"correlator_error:{exc}"
        if not corr:
            macro_driver = str(
                ((drivers[0] if drivers and isinstance(drivers[0], dict) else {}).get("title"))
                or ((drivers[0] if drivers and isinstance(drivers[0], dict) else {}).get("driver"))
                or "Global macro flow"
            ).strip() or "Global macro flow"
            if sector_score > 0.02:
                macro_direction = "tailwind"
            elif sector_score < -0.02:
                macro_direction = "headwind"
            else:
                macro_direction = "neutral"
            if not unavailable_reason and not callable(correlate_stock_news_with_macro):
                unavailable_reason = "correlator_helper_unavailable_macro_only"
            if not unavailable_reason and not snapshot_available:
                unavailable_reason = str(snapshot.get("reason") or "global_news_snapshot_unavailable").strip()
            corr = {
                "driver": macro_driver,
                "direction": macro_direction,
                "confidence": None if math.isnan(sector_conf) else round(sector_conf, 4),
                "evidence": _news_evidence_payload(drivers=drivers, net_score=sector_score),
                "fallback_label": "macro_only_fallback",
            }
        fallback_label = str(corr.get("fallback_label") or "").strip()
        if fallback_label:
            fallback_count += 1
        driver_source = "stock" if stock_news_items and not fallback_label else "macro"
        evidence = corr.get("evidence") if isinstance(corr.get("evidence"), dict) else {}
        if stock_fetch_error and not stock_news_items:
            evidence = {**evidence, "stock_fetch_error": stock_fetch_error}
        audit["news_correlation"] = {
            "driver": str(corr.get("driver") or "Global macro flow"),
            "affected_metric": _infer_news_affected_metric(audit),
            "affected_theme": sector_key,
            "direction": str(corr.get("direction") or "neutral"),
            "confidence": (
                None
                if math.isnan(_safe_float(corr.get("confidence")))
                else round(_safe_float(corr.get("confidence")), 4)
            ),
            "evidence": evidence,
            "fallback_label": fallback_label,
            "driver_source": driver_source,
            "stock_news_fetched_count": len(stock_news_items),
            "stock_news_coverage": coverage_status,
            "used_macro_fallback": bool(fallback_label) or not stock_news_items,
            "available": True,
            "unavailable_reason": unavailable_reason,
            "stock_news": {
                "fetched_count": len(stock_news_items),
                "query_used": str(stock_news_meta.get("query_used") or "").strip(),
                "alias_used": str(stock_news_meta.get("alias_used") or "").strip(),
                "alias_fallback_used": bool(stock_news_meta.get("fallback_used")),
                "fetch_error": str(stock_news_meta.get("error") or "").strip(),
            },
        }
        applied += 1
    return {
        "applied": bool(applied),
        "applied_count": applied,
        "snapshot": snapshot,
        "snapshot_available": snapshot_available,
        "snapshot_reason": str(snapshot.get("reason") or snapshot_reason or "").strip(),
        "sector_news_score": round(sector_score, 4),
        "stock_news_coverage_count": len(stock_news_by_symbol),
        "fallback_count": fallback_count,
    }


def _liquidity_floor_inr() -> float:
    raw = (os.environ.get("TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR") or "").strip()
    if not raw:
        return 1_200_000.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1_200_000.0


def _apply_sector_cross_section(
    ok_results: list[dict[str, Any]], *, score_percentiles: bool = True
) -> None:
    """
    Sector-relative percentiles, ATR penalty scaling vs sector median, liquidity peer rank.

    Call with ``score_percentiles=False`` before recomputing horizon scores, then
    ``score_percentiles=True`` after ``_refresh_symbol_scoring_outputs`` so next-week
    percentiles match the final blended scores.
    """
    audits = [r["audit"] for r in ok_results if isinstance(r.get("audit"), dict)]
    floor = _liquidity_floor_inr()
    if len(audits) < 2:
        for a in audits:
            if not score_percentiles:
                a["sector_cross_section_applied"] = False
                ap = _safe_float(a.get("atr_14_pct"))
                a["atr_penalty_input"] = ap if not math.isnan(ap) else float("nan")
                mn = _safe_float(a.get("median_notional_inr_20d"))
                a["liquidity_thin_proxy"] = bool(not math.isnan(mn) and mn > 0 and mn < floor)
                if a["liquidity_thin_proxy"]:
                    a["liquidity_thin_reason"] = f"median daily notional below ₹{floor:,.0f}"
                else:
                    a.pop("liquidity_thin_reason", None)
            else:
                vals_nw = [_safe_float(a.get("next_week_score"))]
                vals_nd = [_safe_float(a.get("next_day_score"))]
                a["sector_pctile_next_week_score"] = percentile_rank_0_100(
                    [v for v in vals_nw if not math.isnan(v)], vals_nw[0]
                )
                a["sector_pctile_next_day_score"] = percentile_rank_0_100(
                    [v for v in vals_nd if not math.isnan(v)], vals_nd[0]
                )
                a["sector_cross_section_applied"] = True
        return

    def series(key: str) -> list[float]:
        return [_safe_float(a.get(key)) for a in audits]

    def assign_percentile(src: str, dst: str) -> None:
        vals = [v for v in series(src) if not math.isnan(v)]
        for a in audits:
            v = _safe_float(a.get(src))
            a[dst] = percentile_rank_0_100(vals, v) if vals and not math.isnan(v) else float("nan")

    if not score_percentiles:
        assign_percentile("effective_intent_score", "sector_pctile_effective_intent")
        assign_percentile("intent_score", "sector_pctile_intent")
        assign_percentile("z_score", "sector_pctile_z_score")
        assign_percentile("return_1d_pct", "sector_pctile_return_1d_pct")
        assign_percentile("return_5d_pct", "sector_pctile_return_5d_pct")
        assign_percentile("median_notional_inr_20d", "sector_pctile_median_notional_20d")

        atrs = [v for v in series("atr_14_pct") if not math.isnan(v)]
        med_atr = float(median(atrs)) if atrs else float("nan")
        for a in audits:
            a["sector_cross_section_applied"] = False
            a["sector_median_atr_14_pct"] = med_atr
            ap = _safe_float(a.get("atr_14_pct"))
            if not math.isnan(ap) and not math.isnan(med_atr) and med_atr > 1e-9:
                a["atr_penalty_input"] = round(min(5.0, ap / med_atr), 4)
            elif not math.isnan(ap):
                a["atr_penalty_input"] = ap
            else:
                a["atr_penalty_input"] = float("nan")

            mn = _safe_float(a.get("median_notional_inr_20d"))
            sp = _safe_float(a.get("sector_pctile_median_notional_20d"))
            thin_peer = not math.isnan(sp) and sp <= 15.0
            thin_hard = not math.isnan(mn) and mn > 0 and mn < floor
            a["liquidity_thin_proxy"] = bool(thin_hard or thin_peer)
            if thin_peer:
                a["liquidity_thin_reason"] = "bottom-quintile median turnover vs sector peers"
            elif thin_hard:
                a["liquidity_thin_reason"] = f"median daily notional below ₹{floor:,.0f}"
            else:
                a.pop("liquidity_thin_reason", None)
        return

    assign_percentile("next_week_score", "sector_pctile_next_week_score")
    assign_percentile("next_day_score", "sector_pctile_next_day_score")
    for a in audits:
        a["sector_cross_section_applied"] = True


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


def _blend_equity_z_score(close_series: Any) -> tuple[float, float, float | None, str]:
    """
    Blend a 20-session close z-score with a slower window when enough history exists.

    Short windows can flag "bearish deviation" during orderly pullbacks inside a
    slower uptrend; mixing a ~60-session (or max available) z reduces that mismatch
    versus EMA200-style context without dropping the responsive 20d read entirely.
    """
    import pandas as pd

    from titan_engine import calculate_z_score

    s = pd.to_numeric(close_series, errors="coerce").dropna()
    n = len(s)
    win_fast = min(20, max(2, n))
    z_fast = calculate_z_score(s, window=win_fast)
    if n < 45:
        return z_fast, z_fast, None, "20d_only"
    slow_win = min(60, max(21, n - 1))
    z_slow = calculate_z_score(s, window=max(2, slow_win))
    z = round(0.55 * z_fast + 0.45 * z_slow, 4)
    return z, z_fast, z_slow, f"0.55*{win_fast}d+0.45*{slow_win}d"


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
        calculate_adx,
        calculate_atr,
        calculate_atr_ratio,
        calculate_breakout_20d_distances_pct,
        calculate_cmf,
        calculate_ema,
        calculate_equity_technical_score,
        calculate_latest_di,
        calculate_obv_slope,
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
            "return_1d_pct": float("nan"),
            "return_5d_pct": float("nan"),
            "return_10d_pct": float("nan"),
            "return_20d_pct": float("nan"),
            "median_notional_inr_20d": float("nan"),
            "rel_return_5d_vs_nifty_pct": float("nan"),
            "rel_return_10d_vs_nifty_pct": float("nan"),
            "rel_return_20d_vs_nifty_pct": float("nan"),
            "extreme_price_move_proxy": False,
            "adx_14": float("nan"),
            "breakout_20d_distance_pct_to_high": float("nan"),
            "breakout_20d_distance_pct_above_low": float("nan"),
            "atr_14_over_atr_63": float("nan"),
            "cmf_20": float("nan"),
            "cmf_20_delta": {
                "previous_value": None,
                "current_value": None,
                "absolute_change": None,
                "relative_change_percent": None,
                "interpretation": "unavailable",
            },
            "obv_slope_20": float("nan"),
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
    ret5d = pct_return_n_sessions_back(series_non_na, 5)
    ret10d = pct_return_n_sessions_back(series_non_na, 10)
    ret20d = pct_return_n_sessions_back(series_non_na, 20)
    med_notional = median_notional_inr_20d(df, close_col)
    bench = getattr(_THREAD_LOCAL, "sector_benchmark_ohlc", None)
    rel_map = benchmark_relative_returns(df, bench, close_col, horizons=(5, 10, 20))
    extreme_move = False
    if not math.isnan(ret1d) and abs(ret1d) >= 18.0:
        extreme_move = True
    if not math.isnan(ret5d) and abs(ret5d) >= 38.0:
        extreme_move = True
    if not math.isnan(ret10d) and abs(ret10d) >= 48.0:
        extreme_move = True
    z, z_fast, z_slow, z_blend_note = _blend_equity_z_score(series)
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
    adx_14 = calculate_adx(df, window=14)
    adx_plus_di_14, adx_minus_di_14 = calculate_latest_di(df, window=14)
    breakout_to_high, breakout_above_low = calculate_breakout_20d_distances_pct(df)
    atr_14_over_atr_63 = calculate_atr_ratio(df, short_window=14, long_window=63)
    cmf_20 = calculate_cmf(df, window=20)
    cmf_20_prev = calculate_cmf(df.iloc[:-1], window=20) if len(df) > 1 else float("nan")
    cmf_20_delta = _cmf_delta_payload(cmf_20_prev, cmf_20)
    obv_slope_20 = calculate_obv_slope(df, window=20)
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
    high_volume_down_day_proxy = (
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
        "z_score_fast_20": z_fast,
        "z_score_slow": float("nan") if z_slow is None else z_slow,
        "z_score_blend": z_blend_note,
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
        "return_5d_pct": ret5d,
        "return_10d_pct": ret10d,
        "return_20d_pct": ret20d,
        "median_notional_inr_20d": med_notional,
        "extreme_price_move_proxy": extreme_move,
        "rel_return_5d_vs_nifty_pct": rel_map.get("rel_return_5d_vs_nifty_pct", float("nan")),
        "rel_return_10d_vs_nifty_pct": rel_map.get("rel_return_10d_vs_nifty_pct", float("nan")),
        "rel_return_20d_vs_nifty_pct": rel_map.get("rel_return_20d_vs_nifty_pct", float("nan")),
        "ema_200": ema_200,
        "ema_200_distance_pct": ema_distance_pct,
        "atr_14": atr_14,
        "atr_14_pct": atr_14_pct,
        "adx_14": adx_14,
        "adx_plus_di_14": adx_plus_di_14,
        "adx_minus_di_14": adx_minus_di_14,
        "breakout_20d_distance_pct_to_high": breakout_to_high,
        "breakout_20d_distance_pct_above_low": breakout_above_low,
        "atr_14_over_atr_63": atr_14_over_atr_63,
        "cmf_20": cmf_20,
        "cmf_20_delta": cmf_20_delta,
        "obv_slope_20": obv_slope_20,
        "atr_break_multiple": atr_break_multiple,
        "structural_break_proxy": (
            not math.isnan(atr_break_multiple) and atr_break_multiple >= 1.5
        ),
        "high_volume_down_day_proxy": high_volume_down_day_proxy,
        "panic_absorption_proxy": high_volume_down_day_proxy,
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
    _enrich_audit_with_symbol_news(cfg, inst, audit)
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
    import pandas as pd

    from breeze_client import fetch_nifty_data

    try:
        _nifty_df = fetch_nifty_data(cfg, lookback_calendar_days=210)
        _THREAD_LOCAL.sector_benchmark_ohlc = _nifty_df if not _nifty_df.empty else None
    except Exception as ex:
        logger.warning("NIFTY benchmark prefetch skipped: %s", ex)
        _THREAD_LOCAL.sector_benchmark_ohlc = None

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
    heartbeat_seconds = _sector_heartbeat_seconds()
    no_progress_timeout_seconds = max(
        heartbeat_seconds,
        _sector_no_progress_timeout_seconds(),
    )
    watchdog_triggered = False

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_map = {
            pool.submit(worker, cfg, sector_id, inst, event_snapshot=event_snapshot): inst
            for inst in instruments
        }
        pending = set(future_map.keys())
        completed = 0
        last_progress_at = time.monotonic()
        last_heartbeat_at = 0.0
        while pending:
            now = time.monotonic()
            if (now - last_heartbeat_at) >= heartbeat_seconds:
                pending_preview = ", ".join(
                    f"{future_map[f].symbol}:{future_map[f].exchange}"
                    for f in list(pending)[:5]
                ) or "none"
                logger.info(
                    "[Sector] Heartbeat sector=%s done=%s pending=%s no_progress_for=%.1fs "
                    "workers=%s digest=%s pending_preview=%s",
                    sector_id,
                    completed,
                    len(pending),
                    now - last_progress_at,
                    workers,
                    digest,
                    pending_preview,
                )
                last_heartbeat_at = now
            try:
                fut = next(as_completed(pending, timeout=heartbeat_seconds))
            except FutureTimeoutError:
                since_progress = time.monotonic() - last_progress_at
                if since_progress < no_progress_timeout_seconds:
                    continue
                watchdog_triggered = True
                msg = (
                    f"[Sector] no-progress watchdog timeout after {since_progress:.1f}s "
                    f"(threshold={no_progress_timeout_seconds:.1f}s)"
                )
                logger.error("%s; aborting %s pending futures", msg, len(pending))
                for pf in list(pending):
                    inst = future_map[pf]
                    pf.cancel()
                    err_row: dict[str, Any] = {
                        "ok": False,
                        "symbol": inst.symbol,
                        "exchange": inst.exchange,
                        "error": msg,
                        "error_code": "sector_no_progress_watchdog",
                    }
                    if digest:
                        err_row["audit"] = None
                    else:
                        err_row["post"] = ""
                    results.append(err_row)
                pending.clear()
                break

            pending.discard(fut)
            inst = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Sector instrument failed: %s %s", inst.symbol, inst.exchange)
                err_row = {
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
            completed += 1
            last_progress_at = time.monotonic()
    finally:
        pool.shutdown(wait=not watchdog_triggered, cancel_futures=watchdog_triggered)

    if hasattr(_THREAD_LOCAL, "sector_benchmark_ohlc"):
        delattr(_THREAD_LOCAL, "sector_benchmark_ohlc")

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        raise RuntimeError(
            f"[Sector] All {len(results)} instruments failed for sector {sector_id!r}"
        )

    if digest:
        from analysis_store import persist_sector_run_analytics
        from analysis_store import (
            analysis_store_enabled,
            build_comparison_payload,
            build_reconcile_digest_lines,
            enrich_audits_with_stock_reconcile,
            persist_llm_digest_memory,
            quality_checks_for_run,
            update_sector_period_rollups,
        )
        from brain import generate_sector_digest_narrative
        from supabase_log import save_audit_log

        ok_results = [r for r in results if r.get("ok")]
        red_ratio, cluster_downgrades = _apply_cluster_guardrails(ok_results)
        event_adjustments = _apply_event_guardrails(ok_results)
        macro_applied, macro_reason = _apply_macro_guardrails(ok_results, macro_snapshot)
        _apply_sector_cross_section(ok_results, score_percentiles=False)
        for r in ok_results:
            _refresh_symbol_scoring_outputs(r["audit"])
            inst = SectorInstrument(
                symbol=str(r.get("symbol") or ""),
                exchange=str(r.get("exchange") or "NSE"),
            )
            _enrich_audit_with_symbol_news(cfg, inst, r["audit"])
        _apply_sector_cross_section(ok_results, score_percentiles=True)
        news_corr_meta = _apply_global_news_correlation(cfg, sector_id=sector_id, ok_results=ok_results)
        quality_gate = _prediction_quality_gate(ok_results, total_count=len(results))
        audits = [r["audit"] for r in ok_results]
        reconcile_summary = enrich_audits_with_stock_reconcile(
            cfg,
            sector=sector_id,
            audits=audits,
        )
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

        verbose_sections = _digest_verbose_sections_enabled()
        snapshot_meta = news_corr_meta.get("snapshot") if isinstance(news_corr_meta.get("snapshot"), dict) else {}
        snapshot_reason = str(
            news_corr_meta.get("snapshot_reason")
            or snapshot_meta.get("reason")
            or "global_news_snapshot_unavailable"
        ).strip()
        if news_corr_meta.get("snapshot_available"):
            news_snapshot_line = (
                "Global news snapshot: "
                f"{str(snapshot_meta.get('source') or 'unknown_source')} | "
                f"fresh={bool(snapshot_meta.get('fresh'))} | "
                f"age_min={_fmt_metric(snapshot_meta.get('age_minutes'))} | "
                f"refreshed={str(snapshot_meta.get('refreshed_at') or 'unknown_refresh')}"
            )
        else:
            news_snapshot_line = f"Global news snapshot unavailable: {snapshot_reason}"
        applied_count = int(news_corr_meta.get("applied_count", 0) or 0)
        if applied_count > 0:
            news_score_line = (
                f"Global news score ({sector_id}): {_fmt_metric(news_corr_meta.get('sector_news_score'))} "
                f"| correlation_applied={applied_count} symbols"
            )
        else:
            news_score_line = (
                f"Global news score ({sector_id}) unavailable: {snapshot_reason} "
                f"| correlation_applied={applied_count} symbols"
            )

        lines = [
            f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded "
            f"(digest mode: 1 Gemini call)\n",
            "",
            "--- Decision-first top section ---",
            (
                "Deployment mode: ACTIONABLE"
                if quality_gate["passed"]
                else "Deployment mode: WATCHLIST ONLY (quality gate failed)"
            ),
            f"Regime: {(comparison.get('regime') if isinstance(comparison, dict) else 'unknown')}",
            news_snapshot_line,
            news_score_line,
            (
                f"Data reconciliation: requested {len(results)} | success {ok_count} | "
                f"skipped(no data) {len(skipped_rows)} | hard failures {len(hard_failed_rows)}"
            ),
            "",
        ]
        if _digest_reconcile_mode_enabled():
            lines.extend(build_reconcile_digest_lines(reconcile_summary))
        if verbose_sections:
            lines.extend(
                [
                    "",
                    "--- Executive snapshot (verbose) ---",
                    f"Avg effective intent: {_fmt_metric(today.get('avg_effective_intent_score') if isinstance(today, dict) else None)} "
                    f"(vs 7d {_fmt_metric(dlt.get('avg_effective_intent_vs_7d') if isinstance(dlt, dict) else None)}, "
                    f"vs 30d {_fmt_metric(dlt.get('avg_effective_intent_vs_30d') if isinstance(dlt, dict) else None)})",
                    f"Long-term trend breadth (stocks above 200-day Exponential Moving Average): {_fmt_metric(today.get('breadth_above_ema200_pct') if isinstance(today, dict) else None)}%",
                    f"Volume participation breadth (stocks with above-average traded volume): {_fmt_metric(today.get('pct_absorption_gt_1') if isinstance(today, dict) else None)}%",
                    "",
                    "--- Movement summary ---",
                ]
            )
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
                    post.strip(),
                    "",
                    "--- Risk overlays ---",
                    f"Cluster breadth red ratio (<= -1% day): {_fmt_metric(red_ratio * 100.0)}%",
                    f"Cluster bullish downgrades applied: {cluster_downgrades}",
                    f"Event-based risk adjustments applied: {event_adjustments}",
                    (
                        "Macro risk filters: not applied (macro market snapshot unavailable)"
                        if (not macro_applied and str(macro_reason).strip().lower() == "macro snapshot not provided")
                        else f"Macro risk filters: {'applied' if macro_applied else 'not applied'} ({macro_reason})"
                    ),
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
                    f"Average next-week score for top 5 ranked stocks: {_fmt_metric(quality_gate['top5_next_week_mean'])}",
                    f"Score gap between highest-ranked and lowest-ranked stock: {_fmt_metric(quality_gate['spread_top_bottom'])}",
                    (
                        "Gate reasons: none"
                        if quality_gate["passed"]
                        else "Gate reasons: " + "; ".join(quality_gate["reasons"])
                    ),
                    "",
                ]
            )
        lines.append("--- Action summary ---")
        action_counts = {"buy": 0, "hold": 0, "trim": 0, "exit-risk": 0}
        for r in ok_results:
            sig = str((r.get("audit") or {}).get("sell_signal") or "hold").lower()
            if sig not in action_counts:
                sig = "hold"
            action_counts[sig] = action_counts.get(sig, 0) + 1
        lines.extend(
            [
                f"BUY: {action_counts['buy']} | HOLD: {action_counts['hold']} | "
                f"TRIM: {action_counts['trim']} | EXIT RISK: {action_counts['exit-risk']}",
            ]
        )
        report_only_mode = _digest_report_only_mode_enabled()
        if report_only_mode:
            lines.extend(
                [
                    "",
                    "Per-symbol metrics suppressed (report-only reconcile mode).",
                ]
            )
        else:
            lines.extend(
                [
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
        if report_only_mode:
            compact_lines = [
                f"Titan EOD reconcile run: {sector_id!r} — {ok_count}/{len(results)} succeeded",
                "",
            ]
            if _digest_reconcile_mode_enabled():
                compact_lines.extend(build_reconcile_digest_lines(reconcile_summary))
            compact_lines.extend(
                [
                    "",
                    "Report-only enforcement: legacy sector forensic/per-symbol digest blocks suppressed.",
                    (
                        f"Data reconciliation: requested {len(results)} | success {ok_count} | "
                        f"skipped(no data) {len(skipped_rows)} | hard failures {len(hard_failed_rows)}"
                    ),
                ]
            )
            if hard_failure_breakdown:
                compact_lines.append(
                    "Hard-failure breakdown: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(hard_failure_breakdown.items()))
                )
            lines = compact_lines
        digest_text = "\n".join(lines).strip()
        gh_rid = (os.environ.get("GITHUB_RUN_ID") or "").strip() or None
        if analysis_store_enabled():
            mem_run_id = str(persist_meta.get("run_id") or "").strip()
            if not mem_run_id:
                mem_run_id = f"{sector_id}-{datetime.now(IST).strftime('%Y%m%d-%H%M%S')}"
            mem_meta = persist_llm_digest_memory(
                cfg,
                run_id=mem_run_id,
                sector=sector_id,
                prompt_facts={
                    "comparison": comparison if comparison.get("enabled") else {"enabled": False},
                    "reconcile_summary": reconcile_summary,
                },
                output_text=post or digest_text[:8000] or "(digest)",
                model_name=None,
                full_digest=digest_text,
                github_run_id=gh_rid,
            )
            # Visible in GitHub Actions logs for mobile-insights debugging.
            print(
                "TITAN_LLM_DIGEST_MEMORY "
                f"persisted={mem_meta.get('persisted')} "
                f"reason={mem_meta.get('reason', 'ok')} "
                f"sector={sector_id} run_id={mem_run_id} github_run_id={gh_rid or ''} "
                f"analytics_persisted={bool(persist_meta.get('persisted'))}",
                flush=True,
            )
            if not mem_meta.get("persisted"):
                logger.warning(
                    "llm_digest_memory not saved (sector=%s run_id=%s github_run_id=%s): %s",
                    sector_id,
                    mem_run_id,
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
