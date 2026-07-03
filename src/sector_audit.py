"""Sector-wide equity audits: Supabase universe (CSV fallback), parallel Breeze fetches, digest email."""

from __future__ import annotations

import copy
import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig, load_config
from json_util import ensure_utf8_stdio
from sector_registry import SectorInstrument, load_sector_instruments
from market_calendar import is_cash_market_session_open_ist
from tape_metrics import (
    benchmark_relative_returns,
    median_notional_inr_20d,
    ohlc_last_bar_as_of_date,
    percentile_rank_0_100,
    pct_return_n_sessions_back,
    sort_ohlc_by_datetime,
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


def _safe_print(text: str, **kwargs: Any) -> None:
    """Print digest text without crashing on Windows cp1252 consoles."""
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        data = (text if text.endswith("\n") else text + "\n").encode("utf-8", errors="replace")
        sys.stdout.buffer.write(data)
        if kwargs.get("flush"):
            sys.stdout.flush()

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


def _fmt_signed_pct(x: Any, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(v):
        return "n/a"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}"


def _format_price_snapshot_ist(ts: Any) -> str:
    """Render quote LTT as HH:MM IST for digest display."""
    if ts is None:
        return "n/a"
    text = str(ts).strip()
    if not text or text.upper() == "NA":
        return "n/a"
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            else:
                dt = dt.astimezone(IST)
            return dt.strftime("%H:%M IST")
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)
        return dt.strftime("%H:%M IST")
    except ValueError:
        return "n/a"


def _prepare_ohlc_for_metrics(
    df: Any,
    *,
    now_ist: datetime | None = None,
) -> tuple[Any, dict[str, Any]]:
    """
    Sort OHLC rows and, during an open session, drop today's incomplete bar from EOD metrics.
    """
    import pandas as pd

    sorted_df = sort_ohlc_by_datetime(df)
    now = now_ist or datetime.now(IST)
    today = now.date()
    last_bar_date = ohlc_last_bar_as_of_date(sorted_df)
    session_open = is_cash_market_session_open_ist(now)
    ohlc_bar_incomplete = False
    metrics_df = sorted_df

    if session_open and last_bar_date == today and len(sorted_df) >= 2:
        ohlc_bar_incomplete = True
        metrics_df = sorted_df.iloc[:-1].reset_index(drop=True)
        complete_date = ohlc_last_bar_as_of_date(metrics_df)
        ohlc_bar_as_of_date = complete_date.isoformat() if complete_date else None
    else:
        ohlc_bar_as_of_date = last_bar_date.isoformat() if last_bar_date else None

    meta = {
        "ohlc_bar_as_of_date": ohlc_bar_as_of_date,
        "ohlc_bar_incomplete": ohlc_bar_incomplete,
        "session_open": session_open,
        "sorted_df": sorted_df,
        "metrics_df": metrics_df if isinstance(metrics_df, pd.DataFrame) else sorted_df,
    }
    return metrics_df, meta


def _session_move_from_quote(quote: dict[str, Any]) -> tuple[float, str | None]:
    """Display-only session move vs previous close; returns (pct, price_snapshot_ts)."""
    ltp = _safe_float(quote.get("ltp"))
    prev = _safe_float(quote.get("previous_close"))
    ltt = quote.get("ltt")
    pct_raw = _safe_float(quote.get("ltp_percent_change"))
    implied = (
        ((ltp / prev) - 1.0) * 100.0
        if (not math.isnan(ltp) and not math.isnan(prev) and prev > 0)
        else float("nan")
    )
    if not math.isnan(pct_raw) and not math.isnan(implied):
        drift = max(0.25, abs(implied) * 0.05)
        pct = pct_raw if abs(pct_raw - implied) <= drift else implied
    elif not math.isnan(pct_raw):
        pct = pct_raw
    else:
        pct = implied
    ts = str(ltt).strip() if ltt is not None else None
    return pct, ts


def _session_cmf_20_from_ohlc(sorted_df: Any, quote: dict[str, Any] | None) -> float:
    """Display-only CMF on frame including today's partial bar; patches close from quote."""
    from titan_engine import calculate_cmf

    if sorted_df is None or getattr(sorted_df, "empty", True):
        return float("nan")
    df = sorted_df
    if quote:
        ltp = _safe_float(quote.get("ltp"))
        if not math.isnan(ltp):
            df = sorted_df.copy()
            last_idx = df.index[-1]
            df.at[last_idx, "close"] = ltp
            for col, qkey in (("high", "high"), ("low", "low")):
                if col in df.columns:
                    qv = _safe_float(quote.get(qkey))
                    if not math.isnan(qv):
                        df.at[last_idx, col] = qv
            if "high" in df.columns:
                h = _safe_float(df.at[last_idx, "high"])
                if not math.isnan(h):
                    df.at[last_idx, "high"] = max(h, ltp)
            if "low" in df.columns:
                lo = _safe_float(df.at[last_idx, "low"])
                if not math.isnan(lo):
                    df.at[last_idx, "low"] = min(lo, ltp)
    return calculate_cmf(df, window=20)


def _session_volume_participation_ratio(sorted_df: Any, metrics_df: Any) -> float:
    """Display-only VPR: partial today volume vs mean of prior 5 complete sessions."""
    import pandas as pd

    for frame in (sorted_df, metrics_df):
        if frame is None or getattr(frame, "empty", True) or "volume" not in frame.columns:
            return float("nan")
    live_vol = pd.to_numeric(sorted_df["volume"], errors="coerce").dropna()
    prior_vol = pd.to_numeric(metrics_df["volume"], errors="coerce").dropna()
    if live_vol.empty or prior_vol.empty:
        return float("nan")
    current = float(live_vol.iloc[-1])
    tail = prior_vol.tail(5)
    avg = float(tail.mean()) if not tail.empty else float(prior_vol.mean())
    if avg == 0.0:
        return float("inf") if current > 0 else 0.0
    return current / avg


def _volume_participation_label_short(vpr: Any) -> str:
    try:
        v = float(vpr)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if math.isinf(v) and v > 0:
        return "extreme"
    if v >= 1.5:
        return "high"
    if v >= 1.0:
        return "above-avg"
    if v >= 0.7:
        return "below-avg"
    return "thin"


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


def _ema200_distance_bands_text() -> str:
    return (
        "bands: <=10% green, 10-15% yellow, 15-25% orange, >25% red; "
        "-5 to 0 near trend, <-5 below trend"
    )


def _ema200_distance_icon(v: Any) -> str:
    """Digest icon for % distance vs EMA200 (positive = above long-term trend)."""
    f = _safe_float(v)
    if math.isnan(f):
        return "🟡➡"
    if f <= -5.0:
        return "🔴⬇"
    if f < 0.0:
        return "🟡➡"
    if f > 25.0:
        return "🔴⬇"
    if f > 15.0:
        return "🟠➡"
    if f > 10.0:
        return "🟡➡"
    return "🟢⬆"


def _state_for_ema200_distance(v: Any) -> int:
    f = _safe_float(v)
    if math.isnan(f):
        return 0
    if f <= -5.0 or f > 25.0:
        return -1
    if f < 0.0 or f > 15.0:
        return 0
    return 1


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
    states.append(_state_for_ema200_distance(audit.get("ema_200_distance_pct")))
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


def _adx_strength_band_compact(adx_val: Any) -> str:
    adx = _safe_float(adx_val)
    if math.isnan(adx):
        return "n/a"
    if adx >= 25.0:
        return "strong"
    if adx >= 20.0:
        return "weak"
    return "sideways"


def _di_direction_compact(plus_di_val: Any, minus_di_val: Any) -> str:
    plus_di = _safe_float(plus_di_val)
    minus_di = _safe_float(minus_di_val)
    if math.isnan(plus_di) or math.isnan(minus_di):
        return "DI unavailable"
    if plus_di > minus_di:
        return f"+DI {_fmt_metric(plus_di)} > \u2212DI {_fmt_metric(minus_di)}"
    if plus_di < minus_di:
        return f"+DI {_fmt_metric(plus_di)} < \u2212DI {_fmt_metric(minus_di)}"
    return f"+DI {_fmt_metric(plus_di)} = \u2212DI {_fmt_metric(minus_di)}"


def _ema200_distance_band_label(v: Any) -> str:
    f = _safe_float(v)
    if math.isnan(f):
        return "n/a"
    if f <= -5.0:
        return "below"
    if f < 0.0:
        return "near"
    if f <= 10.0:
        return "healthy"
    if f <= 15.0:
        return "mod"
    if f <= 25.0:
        return "extended"
    return "hot"


def _atr_pct_band_label(atr_pct: Any) -> str:
    v = _safe_float(atr_pct)
    if math.isnan(v):
        return "n/a"
    if v < 2.0:
        return "calm"
    if v <= 4.0:
        return "moderate"
    return "elevated"


def _z_label_short(z: Any) -> str:
    try:
        v = float(z)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 2.0:
        return "strong bull"
    if v >= 1.0:
        return "bullish"
    if v <= -2.0:
        return "strong bear"
    if v <= -1.0:
        return "bearish"
    return "mean"


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
        return "n/a"
    if not math.isnan(to_high) and to_high >= -1.0:
        return "near-high"
    if not math.isnan(above_low) and above_low <= 1.0:
        return "near-low"
    return "mid-range"


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


def _digest_show_factor_scores_enabled() -> bool:
    """Render titan_fusion pillar breakdown in digest/email per-symbol blocks (always on)."""
    return True


def _digest_investment_report_enabled() -> bool:
    """Investment report appendix in sector digest (always on)."""
    return True


def _format_fusion_factor_digest_lines(audit: dict[str, Any]) -> list[str]:
    """Optional fusion pillar section; empty when titan_fusion missing."""
    fusion = audit.get("titan_fusion")
    if not isinstance(fusion, dict):
        return []
    lines: list[str] = ["▸ Titan fusion"]
    titan = fusion.get("titan_score")
    conf = fusion.get("overall_confidence")
    if titan is not None:
        conf_txt = f" · conf {_fmt_metric(conf, 3)}" if conf is not None else ""
        lines.append(f"   Titan score {_fmt_metric(titan, 1)}/100{conf_txt}")
    contributions = fusion.get("contributions") if isinstance(fusion.get("contributions"), dict) else {}
    if contributions:
        try:
            from titan_fusion import DISPLAY_LABELS, FUSION_PILLARS
        except ImportError:
            DISPLAY_LABELS = {}
            FUSION_PILLARS = tuple(contributions.keys())
        for pillar in FUSION_PILLARS:
            row = contributions.get(pillar)
            if not isinstance(row, dict):
                continue
            label = DISPLAY_LABELS.get(pillar, pillar)
            w_pct = int(round(float(row.get("weight_effective", 0)) * 100))
            lines.append(
                f"   {label}: {_fmt_metric(row.get('score'), 0)} × {w_pct}% "
                f"= {_fmt_metric(row.get('weighted'), 1)}"
            )
    else:
        for key, label in (
            ("technical_score", "Technical"),
            ("relative_strength_score", "Relative strength"),
            ("flow_score", "Flow"),
            ("fundamental_score", "Fundamentals"),
            ("regime_score", "Regime"),
            ("sector_score", "Sector"),
            ("risk_score", "Risk"),
        ):
            val = fusion.get(key)
            if val is not None:
                lines.append(f"   {label}: {_fmt_metric(val, 0)}")
    expl = fusion.get("overall_explanation")
    if expl and not contributions:
        compact = " | ".join(str(expl).splitlines())
        lines.append(f"   {compact}")
    breadth = fusion.get("breadth_score")
    if breadth is not None:
        lines.append(f"   Breadth (diagnostic): {_fmt_metric(breadth, 0)}")
    return lines


def _append_investment_report_digest_lines(
    lines: list[str],
    ok_results: list[dict[str, Any]],
    *,
    top_n: int = 3,
) -> None:
    if not _digest_investment_report_enabled() or not ok_results:
        return
    try:
        from titan_investment_report import generate_investment_report
    except ImportError:
        return
    ranked = sorted(
        ok_results,
        key=lambda x: (
            float("-inf")
            if math.isnan(_safe_float((x.get("audit") or {}).get("next_week_score", float("nan"))))
            else _safe_float((x.get("audit") or {}).get("next_week_score", float("nan")))
        ),
        reverse=True,
    )
    lines.extend(["", "--- Investment reports (top ranked) ---"])
    for r in ranked[:top_n]:
        audit = r.get("audit")
        if not isinstance(audit, dict):
            continue
        report = generate_investment_report(audit)
        lines.append("")
        lines.append(report.get("markdown") or "")


def _sell_signal_plain_english(signal: str) -> str:
    from action_signals import action_signal_plain_english

    return action_signal_plain_english(signal)


def _action_recommendation_digest_lines(audit: dict[str, Any]) -> list[str]:
    """Recommendation block: conviction score and short-term tilt (headline carries action label)."""
    try:
        from action_engine import action_recommendation_digest_lines

        return action_recommendation_digest_lines(audit)
    except ImportError:
        return []


def _digest_eod_as_of_date(results: list[dict[str, Any]]) -> str | None:
    """Earliest OHLC EOD as-of date across successful symbol audits (digest subject/footer)."""
    dates: list[date] = []
    for row in results:
        if not row.get("ok"):
            continue
        audit = row.get("audit")
        if not isinstance(audit, dict):
            continue
        raw = str(audit.get("ohlc_bar_as_of_date") or "").strip()
        if not raw:
            continue
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue
    if not dates:
        return None
    return min(dates).isoformat()


def _pm_macro_email_enabled(sector_id: str) -> bool:
    """Gate precious-metals macro block in sector digest emails."""
    raw = os.environ.get("TITAN_PM_MACRO_EMAIL")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return sector_id == "metals_mining"


def _build_precious_metals_macro_digest_lines(
    as_of_date: str | None = None,
) -> list[str]:
    """Build PM macro section; never raises (returns unavailable message on failure)."""
    try:
        from pm_macro_data import load_pm_macro_series
        from precious_metals_algo import (
            PreciousMetalsAlgo,
            format_precious_metals_digest_lines,
            resolve_pm_book_value_inr,
        )

        data, pm_notes = load_pm_macro_series()
        if data is None:
            return [
                "--- Precious metals macro ---",
                "Data unavailable — live fetch failed and no CSV fallback",
            ]
        obs = len(next(iter(data.values())))
        z_window = min(252, max(20, obs))
        algo = PreciousMetalsAlgo(z_window=z_window)
        features = algo.generate_features(data)
        result = algo.execute_allocation_logic(features)
        as_of = as_of_date or datetime.now(IST).date().isoformat()
        lines = format_precious_metals_digest_lines(
            result,
            features,
            as_of,
            book_value_inr=resolve_pm_book_value_inr(),
        )
        insert_at = 2
        for note in pm_notes:
            lines.insert(insert_at, note)
            insert_at += 1
        if obs < 252:
            lines.insert(
                insert_at,
                f"Note: using {z_window}-day Z-window ({obs} observations; 252 preferred)",
            )
        return lines
    except Exception as ex:
        logger.warning("Precious metals macro section skipped: %s", ex, exc_info=True)
        return [
            "--- Precious metals macro ---",
            "Data unavailable — live fetch failed and no CSV fallback",
        ]


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


_SHADOW_GATE_LABELS: dict[str, str] = {
    "sector_regime": "regime gate",
    "delivery_churn": "delivery/churn gate",
    "v2_risk_label": "v2 risk gate",
    "futures_oi": "futures OI gate",
    "fno_ban": "ban gate",
    "absorption": "absorption gate",
    "institutional": "institutional gate",
    "signal_overext_ceiling": "overext ceiling gate",
}


def _shadow_gate_display_name(gate_key: str) -> str:
    key = str(gate_key or "").strip().lower()
    return _SHADOW_GATE_LABELS.get(key, key.replace("_", " ") + " gate" if key else "gate")


def _shadow_gate_would_action(record: dict[str, Any]) -> str:
    if bool(record.get("withhold")):
        return "withhold"
    would = str(record.get("would") or "").strip().lower()
    if "cap" in would:
        return "cap"
    try:
        mult = float(record.get("score_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    if "withhold" in would and "damp" in would:
        return "damp"
    if mult < 1.0 and "damp" in would:
        return "damp"
    if "withhold" in would or "skip" in would:
        return "withhold"
    if "damp" in would or "down-rank" in would:
        return "damp"
    if mult < 1.0:
        return "damp"
    return "damp"


def _shadow_gate_reason_brief(record: dict[str, Any], audit: dict[str, Any]) -> str:
    reasons = record.get("reasons")
    if isinstance(reasons, list) and reasons:
        return "; ".join(str(r) for r in reasons[:2])

    gate = str(record.get("gate") or "").strip().lower()
    if gate == "sector_regime":
        sector = str(audit.get("sector_key") or audit.get("sector_id") or "").strip()
        breadth = record.get("breadth_now")
        if sector and breadth is not None:
            return f"{sector} breadth {float(breadth):.0f}%"
        if breadth is not None:
            return f"breadth {float(breadth):.0f}%"
    if gate == "v2_risk_label":
        label = str(record.get("v2_label") or "").strip()
        if label:
            return f"signal_v2 label {label}"
    if gate == "delivery_churn":
        avg = record.get("delivery_avg")
        latest = record.get("delivery_latest")
        if avg is not None and latest is not None:
            return f"avg delivery {float(avg):.0f}%->{float(latest):.0f}%"
    if gate == "futures_oi":
        structure = str(record.get("structure") or "").strip()
        oi_chg = record.get("change_in_oi")
        if structure and structure != "unknown":
            suffix = f" (ΔOI {oi_chg})" if oi_chg is not None else ""
            return f"{structure.replace('_', ' ')}{suffix}"
    if gate == "fno_ban":
        ban_date = record.get("ban_date")
        return f"F&O ban list as of {ban_date}" if ban_date else "on F&O ban list"
    if gate == "signal_overext_ceiling":
        hot = record.get("hot")
        if isinstance(hot, list) and hot:
            return ", ".join(str(x) for x in hot[:3])
        ceiling = record.get("would_ceiling")
        if ceiling:
            return f"would cap to {ceiling}"
    would = str(record.get("would") or "").strip()
    return would if would and would.lower() != "allow" else "shadow trigger"


def _gate_record_applied(record: dict[str, Any]) -> bool:
    """True when a gate record is triggered and its mode is actively enforced."""
    if bool(record.get("applied")):
        return True
    if not bool(record.get("triggered")):
        return False
    mode = str(record.get("mode") or "shadow").strip().lower()
    gate = str(record.get("gate") or "").strip().lower()
    if gate == "signal_overext_ceiling" and mode in ("enforce", "damp") and record.get("applied_ceiling"):
        return True
    if gate == "absorption" and mode in ("damp", "enforce"):
        return True
    if mode in ("off", "shadow"):
        return False
    if mode in ("damp", "skip", "enforce"):
        return True
    if bool(record.get("withhold")):
        return True
    try:
        mult = float(record.get("score_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    return mult < 1.0 - 1e-9


def _format_applied_gate_note(record: dict[str, Any], audit: dict[str, Any]) -> str:
    gate_name = _shadow_gate_display_name(str(record.get("gate") or ""))
    reason = _shadow_gate_reason_brief(record, audit)
    gate = str(record.get("gate") or "").strip().lower()
    if bool(record.get("withhold")):
        return f"Applied: {gate_name} — withheld from priority — {reason}"
    if gate == "signal_overext_ceiling":
        ceiling = record.get("applied_ceiling") or record.get("would_ceiling")
        return f"Applied: {gate_name} — label capped to {ceiling} — {reason}"
    try:
        mult = float(record.get("score_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    if mult < 1.0 - 1e-9:
        mult_txt = f"×{mult:.2f}" if mult > 0 else "×0"
        return f"Applied: {gate_name} — rank damped ({mult_txt}) — {reason}"
    action = _shadow_gate_would_action(record)
    return f"Applied: {gate_name} — {action} — {reason}"


def _format_gate_digest_note(record: dict[str, Any], audit: dict[str, Any]) -> str:
    if _gate_record_applied(record):
        return _format_applied_gate_note(record, audit)
    return _format_shadow_gate_note(record, audit)


def _format_shadow_gate_note(record: dict[str, Any], audit: dict[str, Any]) -> str:
    gate_name = _shadow_gate_display_name(str(record.get("gate") or ""))
    action = _shadow_gate_would_action(record)
    reason = _shadow_gate_reason_brief(record, audit)
    return f"Shadow (not enforced): {gate_name} would {action} — {reason}"


def _overext_digest_should_show(oe: dict[str, Any]) -> bool:
    return bool(oe.get("would_ceiling"))


def _overext_ceiling_shadow_triggered(oe: dict[str, Any]) -> bool:
    return _overext_digest_should_show(oe)


def _absorption_digest_should_show(abs_term: dict[str, Any]) -> bool:
    mode = str(abs_term.get("mode") or "shadow").strip().lower()
    legacy = _safe_float(abs_term.get("legacy"))
    gated = _safe_float(abs_term.get("gated"))
    value = _safe_float(abs_term.get("value"))
    if bool(abs_term.get("down_day")) and legacy > 0.0:
        return True
    if bool(abs_term.get("capped")) and not math.isclose(legacy, gated, rel_tol=0.0, abs_tol=0.05):
        return True
    if mode in ("damp", "enforce") and not math.isclose(legacy, value, rel_tol=0.0, abs_tol=0.05):
        return True
    return False


def _absorption_shadow_triggered(abs_term: dict[str, Any]) -> bool:
    return _absorption_digest_should_show(abs_term)


def _absorption_shadow_record(abs_term: dict[str, Any]) -> dict[str, Any]:
    legacy = _safe_float(abs_term.get("legacy"))
    gated = _safe_float(abs_term.get("gated"))
    reasons: list[str] = []
    if bool(abs_term.get("down_day")) and legacy > 0.0:
        reasons.append(f"down-day distribution bonus {legacy:+.1f} pts would zero")
    elif bool(abs_term.get("capped")):
        reasons.append(f"uncapped bonus {legacy:+.1f} pts would cap to {gated:+.1f}")
    mode = str(abs_term.get("mode") or "shadow").strip().lower()
    would = "damp (sign-gated absorption)" if mode in ("damp", "enforce") else "cap (sign-gated absorption)"
    record: dict[str, Any] = {
        "gate": "absorption",
        "mode": mode,
        "triggered": True,
        "would": would,
        "reasons": reasons,
        "score_multiplier": 0.5 if mode == "damp" else 1.0,
        "withhold": False,
    }
    if mode in ("damp", "enforce"):
        record["applied"] = True
    return record


def _institutional_shadow_triggered(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("risk_off"))


def _institutional_shadow_record(ctx: dict[str, Any]) -> dict[str, Any]:
    fii = ctx.get("fii_net_crs")
    dii = ctx.get("dii_net_crs")
    parts: list[str] = []
    if fii is not None:
        parts.append(f"FII net {float(fii):+.0f} Cr")
    if dii is not None:
        parts.append(f"DII net {float(dii):+.0f} Cr")
    reason = ", ".join(parts) if parts else "risk-off institutional backdrop"
    mode = str(ctx.get("mode") or "shadow").strip().lower()
    mult = _safe_float(ctx.get("score_multiplier"))
    if math.isnan(mult):
        mult = 1.0
        if mode == "damp":
            mult = 0.85
        elif mode == "skip":
            mult = 0.85
    withhold = bool(ctx.get("withhold"))
    if mode == "skip" and not withhold:
        withhold = True
    record: dict[str, Any] = {
        "gate": "institutional",
        "mode": mode,
        "triggered": True,
        "would": "damp (institutional backdrop)",
        "reasons": [reason],
        "score_multiplier": mult,
        "withhold": withhold,
    }
    if bool(ctx.get("gate_applied")) or mode in ("damp", "skip", "enforce"):
        record["applied"] = True
    return record


def _digest_shadow_gate_notes(audit: dict[str, Any]) -> list[str]:
    """Per-stock gate lines for digest Context (shadow preview or applied enforcement)."""
    from sector_priority import rehydrate_institutional_context, rehydrate_persisted_gate_records

    if isinstance(audit.get("shadow_gates"), list):
        audit["shadow_gates"] = rehydrate_persisted_gate_records(audit["shadow_gates"])
    if isinstance(audit.get("institutional_context"), dict):
        audit["institutional_context"] = rehydrate_institutional_context(
            audit["institutional_context"]
        )

    notes: list[str] = []
    seen: set[str] = set()

    def _append(record: dict[str, Any]) -> None:
        gate = str(record.get("gate") or "").strip().lower()
        if not gate or gate in seen:
            return
        if not bool(record.get("triggered")):
            return
        seen.add(gate)
        notes.append(_format_gate_digest_note(record, audit))

    for record in audit.get("shadow_gates") or []:
        if isinstance(record, dict):
            _append(record)

    oe = audit.get("signal_overext_ceiling")
    if isinstance(oe, dict) and _overext_digest_should_show(oe):
        mode = str(oe.get("mode") or "shadow").strip().lower()
        applied = oe.get("applied_ceiling")
        would = oe.get("would_ceiling")
        _append(
            {
                "gate": "signal_overext_ceiling",
                "mode": mode,
                "triggered": True,
                "would": f"cap to {would}",
                "would_ceiling": would,
                "applied_ceiling": applied,
                "hot": oe.get("hot"),
                "withhold": False,
                "applied": bool(mode in ("enforce", "damp") and applied),
            }
        )

    abs_term = audit.get("absorption_term_shadow")
    if isinstance(abs_term, dict) and _absorption_shadow_triggered(abs_term):
        _append(_absorption_shadow_record(abs_term))

    inst_ctx = audit.get("institutional_context")
    if isinstance(inst_ctx, dict) and _institutional_shadow_triggered(inst_ctx):
        _append(_institutional_shadow_record(inst_ctx))

    return notes


def _load_priority_ranking_meta(
    cfg: TitanConfig,
    *,
    sector_key: str,
    symbol_keys: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Best-effort lookup of ranking meta (shadow_gates, absorption, institutional)."""
    syms = sorted({sym for sym, _ex in symbol_keys if sym})
    if not syms or not sector_key:
        return {}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of_today = datetime.now(IST).date().isoformat()
    for as_of in (as_of_today,):
        try:
            res = (
                client.table("sector_priority_rankings")
                .select("symbol,exchange,meta")
                .eq("sector_key", sector_key.strip().lower())
                .eq("as_of_date", as_of)
                .in_("symbol", syms)
                .execute()
            )
        except APIError as exc:
            payload = exc.args[0] if exc.args else {}
            msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            if code == "PGRST205" or "could not find the table" in msg.lower():
                return {}
            logger.info("priority ranking meta read failed sector=%s: %s", sector_key, exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.info("priority ranking meta read failed sector=%s: %s", sector_key, exc)
            return {}
        rows = list(getattr(res, "data", None) or [])
        if not rows:
            continue
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            ex = str(row.get("exchange") or "NSE").strip().upper()
            meta = row.get("meta")
            if sym and isinstance(meta, dict):
                out[(sym, ex)] = meta
        if out:
            return out
    try:
        res = (
            client.table("sector_priority_rankings")
            .select("symbol,exchange,meta,as_of_date")
            .eq("sector_key", sector_key.strip().lower())
            .in_("symbol", syms)
            .order("as_of_date", desc=True)
            .limit(max(50, len(syms) * 3))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("priority ranking meta fallback read failed sector=%s: %s", sector_key, exc)
        return {}
    rows = list(getattr(res, "data", None) or [])
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        ex = str(row.get("exchange") or "NSE").strip().upper()
        key = (sym, ex)
        if key in out or not sym:
            continue
        meta = row.get("meta")
        if isinstance(meta, dict):
            out[key] = meta
    return out


def _bridge_priority_shadow_context(
    cfg: TitanConfig,
    sector_key: str,
    ok_results: list[dict[str, Any]],
) -> None:
    """Attach persisted ranking shadow-gate meta onto audits for digest rendering."""
    if not ok_results:
        return
    symbol_keys = [
        (str(r.get("symbol") or "").strip().upper(), str(r.get("exchange") or "NSE").strip().upper())
        for r in ok_results
    ]
    meta_by_key = _load_priority_ranking_meta(cfg, sector_key=sector_key, symbol_keys=symbol_keys)
    from sector_priority import rehydrate_institutional_context, rehydrate_persisted_gate_records

    for r in ok_results:
        audit = r.get("audit")
        if not isinstance(audit, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        ex = str(r.get("exchange") or "NSE").strip().upper()
        from sector_priority import enrich_audit_sector_signal_profile

        enrich_audit_sector_signal_profile(audit, sector_key)
        meta = meta_by_key.get((sym, ex)) or {}
        if isinstance(meta.get("shadow_gates"), list) and not audit.get("shadow_gates"):
            audit["shadow_gates"] = list(meta["shadow_gates"])
        if isinstance(audit.get("shadow_gates"), list):
            audit["shadow_gates"] = rehydrate_persisted_gate_records(audit["shadow_gates"])
        if isinstance(meta.get("absorption_term"), dict) and not audit.get("absorption_term_shadow"):
            audit["absorption_term_shadow"] = meta["absorption_term"]
        if isinstance(meta.get("institutional_context"), dict) and not audit.get("institutional_context"):
            audit["institutional_context"] = dict(meta["institutional_context"])
        if isinstance(audit.get("institutional_context"), dict):
            audit["institutional_context"] = rehydrate_institutional_context(
                audit["institutional_context"]
            )


def _format_sector_options_context_block(ctx: dict[str, Any]) -> list[str]:
    """Sector-level options header for digest email (Phase 1)."""
    if not isinstance(ctx, dict) or not ctx:
        return []
    if bool(ctx.get("sector_option_chain_unavailable", True)):
        return [
            "▸ Sector options context",
            "  Options chain unavailable for sector benchmark index.",
        ]
    underlying = str(ctx.get("sector_options_underlying") or "NIFTY")
    lines = [
        "▸ Sector options context",
        (
            f"  {underlying} PCR {_fmt_metric(ctx.get('sector_pcr'), 2)} "
            f"| put wall {_fmt_metric(ctx.get('sector_put_wall_strike'), 0)} "
            f"| call wall {_fmt_metric(ctx.get('sector_call_wall_strike'), 0)} "
            f"| expiry {str(ctx.get('sector_options_expiry') or 'n/a')}"
        ),
    ]
    spot = _safe_float(ctx.get("sector_index_spot"))
    if not math.isnan(spot):
        lines.append(
            f"  Index spot {_fmt_metric(spot, 2)} "
            f"| vs put wall {_fmt_signed_pct(ctx.get('sector_spot_vs_put_wall_pct'))}% "
            f"| vs call wall {_fmt_signed_pct(ctx.get('sector_spot_vs_call_wall_pct'))}%"
        )
    return lines


def _format_symbol_options_context_block(audit: dict[str, Any]) -> list[str]:
    """Per-symbol options block for digest email (Phase 2)."""
    if bool(audit.get("option_chain_unavailable", True)):
        if audit.get("option_chain_not_fno"):
            return [
                "▸ Options context",
                "  not in F&O universe (display only)",
            ]
        if audit.get("option_chain_fetch_attempted"):
            reason = str(audit.get("option_chain_unavailable_reason") or "chain unavailable").strip()
            return [
                "▸ Options context",
                f"  chain unavailable ({reason})",
            ]
        return []
    lines = [
        "▸ Options context",
        (
            f"  PCR {_fmt_metric(audit.get('pcr'), 2)} "
            f"| put wall {_fmt_metric(audit.get('put_oi_wall_strike'), 0)} "
            f"| call wall {_fmt_metric(audit.get('call_oi_wall_strike'), 0)} "
            f"| expiry {str(audit.get('options_expiry') or audit.get('option_expiry') or 'n/a')}"
        ),
    ]
    spot = _safe_float(audit.get("close_last"))
    if not math.isnan(spot):
        lines.append(
            f"  Spot {_fmt_metric(spot, 2)} "
            f"| vs put wall {_fmt_signed_pct(audit.get('spot_vs_put_wall_pct'))}% "
            f"| vs call wall {_fmt_signed_pct(audit.get('spot_vs_call_wall_pct'))}%"
        )
    from options_context import options_confirmation_note

    note = options_confirmation_note(audit)
    if note:
        lines.append(f"  {note}")
    return lines


def _prediction_brief_line(audit: dict[str, Any], *, compact: bool = False) -> str:
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

    if compact:
        parts = [f"Model read: {confidence} confidence"]
        if drv:
            parts.append(f"{drv} supportive")
        if drag:
            parts.append(f"{drag} weighing on the score")
        if penalties:
            parts.append(f"flags: {'; '.join(str(p) for p in penalties[:2])}")
        return " · ".join(parts)

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
    from options_context import options_confirmation_note

    opt_note = options_confirmation_note(audit)
    if opt_note:
        parts.append(opt_note)
    return " · ".join(parts)


def _prediction_confidence_bands_line(*, compact: bool = False) -> str:
    if compact:
        return "   Confidence: \u226570 high \u00b7 55\u201369 medium \u00b7 <55 low"
    return ""


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


def _news_evidence_line(audit: dict[str, Any], *, compact: bool = False) -> str:
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

    def _headline_snippet(name: str) -> str:
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
        row = rows[0] if isinstance(rows[0], dict) else {}
        headline = str(row.get("headline") or "").strip()
        if not headline:
            return f"{name}=none"
        source = str(row.get("source") or "unknown").strip()
        if compact:
            return f"{name}: {headline[:60]}{'…' if len(headline) > 60 else ''} ({source})"
        impact = _fmt_metric(row.get("impact_contribution_score"), 4)
        published_at = str(row.get("published_at") or "n/a").strip() or "n/a"
        return (
            f"{name}={headline} [source={source}; published_at={published_at}; "
            f"impact_contribution_score={impact}]"
        )

    if compact:
        snippets = [_headline_snippet(b) for b in ("global", "stock", "local", "market")]
        active = [s for s in snippets if not s.endswith("=none") and "none (" not in s]
        stock_none = next((s for s in snippets if s.startswith("stock=none")), "")
        parts = [f"News evidence: impact {_fmt_metric(net_score, 4)} ({direction})"]
        parts.extend(active)
        if stock_none:
            parts.append(stock_none)
        return " · ".join(parts)

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


def _news_correlation_line(audit: dict[str, Any], *, compact: bool = False) -> str:
    corr = audit.get("news_correlation")
    if not isinstance(corr, dict):
        return "News correlation unavailable: correlation metadata missing"
    unavailable_reason = str(corr.get("unavailable_reason") or "").strip()
    if unavailable_reason:
        if compact:
            return f"News: unavailable ({unavailable_reason})"
        return f"News correlation unavailable: {unavailable_reason}"
    if corr.get("available") is False:
        if compact:
            return "News: unavailable (correlation data unavailable)"
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
        if compact:
            return "News: unavailable (incomplete payload)"
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
        if compact:
            return (
                f"Stock news: {driver} · {dir_label} · conf {conf_txt} ({conf_band}) · "
                f"{metric} · fetched={stock_news_fetched_count}"
            )
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
    if compact:
        fb = fallback_label.replace("sector_specific_match_missing_", "") if fallback_label else fallback_reason
        line = (
            f"Macro: {driver} · {dir_label} · conf {conf_txt} ({conf_band}) · {metric}"
        )
        if fb:
            line += f" · fallback={fb}"
        if fetch_error:
            line += f" · stock_fetch_error={fetch_error}"
        return line
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

    preserved_shadow_gates = (
        copy.deepcopy(audit["shadow_gates"])
        if isinstance(audit.get("shadow_gates"), list)
        else None
    )
    preserved_overext = (
        copy.deepcopy(audit["signal_overext_ceiling"])
        if isinstance(audit.get("signal_overext_ceiling"), dict)
        else None
    )

    def _restore_persisted_gate_context() -> None:
        if preserved_shadow_gates is not None:
            audit["shadow_gates"] = preserved_shadow_gates
        if preserved_overext is not None:
            audit["signal_overext_ceiling"] = preserved_overext

    lines_out: list[str] = []
    try:
        from action_engine import derive_full_action, digest_headline_text

        action = derive_full_action(audit)
        _restore_persisted_gate_context()
        audit["full_action"] = action
        lines_out.append(f"{symbol} ({exchange}) — {digest_headline_text(audit, action)}")
        rec_lines = _action_recommendation_digest_lines(audit)
        _restore_persisted_gate_context()
        if rec_lines:
            lines_out.extend(rec_lines)
    except ImportError:
        sell_signal = audit.get("sell_signal", "unknown")
        lines_out.append(f"{symbol} ({exchange}) — {_sell_signal_plain_english(str(sell_signal))}")
    try:
        from action_engine import format_buy_checklist_digest_line

        buy_line = format_buy_checklist_digest_line(audit)
        if buy_line:
            lines_out.append(f"   {buy_line}")
    except ImportError:
        risk_net = _safe_float(audit.get("sell_signal_risk_score"))
        if str(sell_signal).lower() == "hold" and not math.isnan(risk_net) and risk_net < 4.0:
            nw_gate = 65.0
            intent_gate = 60.0
            nw_val = _safe_float(next_week)
            intent_val = _safe_float(intent)
            lines_out.append(
                f"   Buy gate: next_week {_fmt_metric(nw_val)}/{nw_gate:.0f}, "
                f"intent {_fmt_metric(intent_val)}/{intent_gate:.0f}"
            )

    lines_out.append("▸ Trend Regime (14D)")
    trend_regime = _trend_regime_label(adx_14, plus_di_14, minus_di_14)
    trend_icon = "🟡➡"
    if trend_regime == "Buy trend":
        trend_icon = "🟢⬆"
    elif trend_regime == "Sell trend":
        trend_icon = "🔴⬇"
    range_ctx = _range_position_context(breakout_to_high, breakout_above_low)
    lines_out.append(
        f"{trend_icon} Regime: {trend_regime} · ADX {_fmt_metric(adx_14)} "
        f"({_adx_strength_band_compact(adx_14)}) · "
        f"{_di_direction_compact(plus_di_14, minus_di_14)}"
    )
    lines_out.append(
        f"{_breakout_state_icon(breakout_to_high, breakout_above_low)} "
        f"20D range: {_fmt_signed_pct(breakout_to_high)}% to high · "
        f"{_fmt_signed_pct(breakout_above_low)}% above low · {range_ctx}"
    )
    lines_out.append(
        f"{_ema200_distance_icon(ema_dist)} "
        f"EMA200: {_fmt_signed_pct(ema_dist)}% · {_ema200_distance_band_label(ema_dist)}"
    )
    lines_out.append("   ADX strength: <20 sideways · 20–24 weak · ≥25 strong")
    lines_out.append("   20D range: near-high ≥−1% · near-low ≤+1% to low")
    lines_out.append(
        "   EMA200: ≤10% healthy · 10–15% mod · 15–25% extended · >25% hot · "
        "−5–0 near · <−5 below"
    )

    lines_out.append("▸ 20D Money Flow")
    ohlc_as_of_mf = str(audit.get("ohlc_bar_as_of_date") or "").strip()
    mf_eod_as_of = ohlc_as_of_mf if ohlc_as_of_mf else "n/a"
    cmf_display = cmf_label if cmf_label != "CMF20" else "CMF"
    lines_out.append(
        f"{_metric_icon(cmf_val, bullish_above=0.05, bearish_below=-0.05)} "
        f"{cmf_display} (EOD): {_fmt_metric(cmf_val, 3)} {_cmf_band(cmf_val)} · as of {mf_eod_as_of}"
    )
    session_cmf = audit.get("session_cmf_20")
    if (
        cmf_label == "CMF20"
        and audit.get("price_snapshot_ts")
        and not math.isnan(_safe_float(session_cmf))
    ):
        snap_cmf = _format_price_snapshot_ist(audit.get("price_snapshot_ts"))
        session_cmf_f = _safe_float(session_cmf)
        lines_out.append(
            f"{_metric_icon(session_cmf_f, bullish_above=0.05, bearish_below=-0.05)} "
            f"CMF (live): {_fmt_metric(session_cmf_f, 3)} {_cmf_band(session_cmf_f)} · as of {snap_cmf}"
        )
    lines_out.append("   CMF: >0.05 acc · −0.05 to 0.05 neutral · <−0.05 dist")
    vpr_eod = audit.get("volume_participation_ratio", audit.get("absorption_ratio"))
    vpr_eod_f = _safe_float(vpr_eod)
    lines_out.append(
        f"{_metric_icon(vpr_eod_f, bullish_above=1.5, bearish_below=0.7)} "
        f"VPR (EOD): {_fmt_metric(vpr_eod)}x {_volume_participation_label_short(vpr_eod)} · "
        f"as of {mf_eod_as_of}"
    )
    session_vpr = audit.get("session_volume_participation_ratio")
    if audit.get("price_snapshot_ts") and not math.isnan(_safe_float(session_vpr)):
        snap_vpr = _format_price_snapshot_ist(audit.get("price_snapshot_ts"))
        session_vpr_f = _safe_float(session_vpr)
        lines_out.append(
            f"{_metric_icon(session_vpr_f, bullish_above=1.5, bearish_below=0.7)} "
            f"VPR (live): {_fmt_metric(session_vpr)}x {_volume_participation_label_short(session_vpr)} · "
            f"as of {snap_vpr}"
        )
    lines_out.append("   VPR: ≥1.5 high · 1.0–1.49 above-avg · 0.7–0.99 below · <0.7 thin")
    sp_int = audit.get("sector_pctile_effective_intent")
    lines_out.append(
        f"{_metric_icon(sp_int, bullish_above=67.0, bearish_below=33.0)} "
        f"Intent percentile: {_fmt_metric(sp_int)} {_sector_rank_band(sp_int)}"
    )
    sp_nw = audit.get("sector_pctile_next_week_score")
    lines_out.append(
        f"{_metric_icon(sp_nw, bullish_above=67.0, bearish_below=33.0)} "
        f"1W outlook percentile: {_fmt_metric(sp_nw)} {_sector_rank_band(sp_nw)}"
    )
    lines_out.append("   Percentile: ≥67 leader · 34–66 average · ≤33 laggard")

    lines_out.append("▸ 1D / Tape")
    ohlc_as_of = str(audit.get("ohlc_bar_as_of_date") or "").strip()
    eod_as_of = ohlc_as_of if ohlc_as_of else "n/a"
    lines_out.append(
        f"{_metric_icon(ret1d, bullish_above=1.0, bearish_below=-1.0)} "
        f"1D move (EOD): {_fmt_signed_pct(ret1d)}% · as of {eod_as_of}"
    )
    session_move = audit.get("session_move_vs_prev_close_pct")
    if audit.get("price_snapshot_ts") and not math.isnan(_safe_float(session_move)):
        snap = _format_price_snapshot_ist(audit.get("price_snapshot_ts"))
        lines_out.append(
            f"{_metric_icon(session_move, bullish_above=1.0, bearish_below=-1.0)} "
            f"Session move (live): {_fmt_signed_pct(session_move)}% · as of {snap}"
        )
    z_fast = audit.get("z_score_fast_20", z)
    z_slow = audit.get("z_score_slow")
    z_bands = (
        "   Z bands: ≥+2 strong bull · +1 to +2 bull · −1 to +1 mean · "
        "−2 to −1 bear · ≤−2 strong bear"
    )
    z_slow_finite = math.isfinite(_safe_float(z_slow))
    if z_slow_finite:
        lines_out.append(
            f"{_metric_icon(z_fast, bullish_above=1.0, bearish_below=-1.0)} "
            f"1D z-score (20D): {_fmt_signed_pct(z_fast)} {_z_label_short(z_fast)} · as of {eod_as_of}"
        )
        lines_out.append(
            f"{_metric_icon(z_slow, bullish_above=1.0, bearish_below=-1.0)} "
            f"1D z-score (60D): {_fmt_signed_pct(z_slow)} {_z_label_short(z_slow)} · as of {eod_as_of}"
        )
        lines_out.append(z_bands)
        lines_out.append(
            f"{_metric_icon(z, bullish_above=1.0, bearish_below=-1.0)} "
            f"1D z-score (blend, scoring): {_fmt_signed_pct(z)} {_z_label_short(z)} · as of {eod_as_of}"
        )
    else:
        lines_out.append(
            f"{_metric_icon(z_fast, bullish_above=1.0, bearish_below=-1.0)} "
            f"1D z-score (20D): {_fmt_signed_pct(z_fast)} {_z_label_short(z_fast)} · "
            f"blend (scoring): {_fmt_signed_pct(z)} · as of {eod_as_of}"
        )
        lines_out.append(z_bands)
    atr_icon = "🟡➡"
    atr_v = _safe_float(atr_pct)
    if not math.isnan(atr_v):
        if atr_v < 2.0:
            atr_icon = "🟢⬆"
        elif atr_v > 4.0:
            atr_icon = "🔴⬇"
    lines_out.append(
        f"{atr_icon} ATR14: {_fmt_metric(atr_pct)}% {_atr_pct_band_label(atr_pct)} · "
        f"vs 3M: {_fmt_metric(atr_ratio)}x {_atr_ratio_band(atr_ratio)}"
    )
    lines_out.append("   ATR14: <2 calm · 2–4 moderate · >4 elevated")
    lines_out.append("   ATR ratio: <0.90 low · 0.90–1.10 normal · >1.10 high")

    options_lines = _format_symbol_options_context_block(audit)
    if options_lines:
        lines_out.extend(options_lines)

    lines_out.append("▸ Model outlook")
    lines_out.append(
        f"{_metric_icon(intent, bullish_above=55.0, bearish_below=45.0)} "
        f"Technical intent: {_fmt_metric(intent)}/100 · {_equity_technical_label(intent)}"
    )
    nw_l = _horizon_score_label(next_week)
    lines_out.append(
        f"{_metric_icon(next_week, bullish_above=55.0, bearish_below=45.0)} "
        f"1W outlook: {_fmt_metric(next_week)}/100 · {nw_l}"
    )
    nd_l = _horizon_score_label(nf)
    lines_out.append(
        f"{_metric_icon(nf, bullish_above=55.0, bearish_below=45.0)} "
        f"1D outlook: {_fmt_metric(nf)}/100 · {nd_l}"
    )
    lines_out.append(
        "   Intent bands: ≥70 high-long · 55–69 mod-long · 45–54 neutral · "
        "30–44 defensive · <30 high-defensive"
    )
    lines_out.append(
        "   Horizon bands: ≥70 strong · 55–69 constructive · 45–54 neutral · "
        "35–44 caution · <35 defensive"
    )

    if sell_reasons:
        lines_out.append("Why this action:")
        for reason in sell_reasons[:3]:
            lines_out.append(f"   • {reason}")

    pred = _prediction_brief_line(audit, compact=True)
    if pred:
        lines_out.append(pred)
        lines_out.append(_prediction_confidence_bands_line(compact=True))

    context_tail: list[str] = []
    cmf_delta = _cmf_delta_line(audit)
    if cmf_delta:
        context_tail.append(cmf_delta)

    news_rel = _news_correlation_line(audit, compact=True)
    if news_rel:
        context_tail.append(news_rel)
    news_evidence = _news_evidence_line(audit, compact=True)
    if news_evidence:
        context_tail.append(news_evidence)

    flag_simple = _digest_flags_simple(audit)
    for flag in flag_simple:
        context_tail.append(f"• {flag}")

    _restore_persisted_gate_context()
    for shadow_note in _digest_shadow_gate_notes(audit):
        context_tail.append(f"• {shadow_note}")

    if support_tag != "technical_only":
        context_tail.append(f"• Evidence mix: {support_tag.replace('_', ' ')}")

    if fundamental_status.lower() not in ("unavailable", "na", "n/a", "") and not str(fundamental_status).startswith(
        "unavailable",
    ):
        fr = "; ".join(str(x) for x in fundamental_reasons[:2]) if fundamental_reasons else ""
        context_tail.append(
            f"• Fundamentals: {fundamental_status} ({_fmt_metric(fundamental_score)})"
            + (f" — {fr}" if fr else ""),
        )
    elif fundamental_status.lower() in ("unavailable", "na", "n/a", "") or str(fundamental_status).startswith(
        "unavailable",
    ):
        context_tail.append("• Fundamentals: unavailable (run ingest_fundamentals)")

    if fallback_used and exchange_used.upper() != str(exchange).upper():
        context_tail.append(f"• Price feed: {exchange_used} (alternate to {exchange})")

    if context_tail:
        lines_out.append("▸ Context")
        lines_out.extend(context_tail)

    if _digest_show_factor_scores_enabled():
        fusion_lines = _format_fusion_factor_digest_lines(audit)
        if fusion_lines:
            lines_out.extend(fusion_lines)

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


# ---------------------------------------------------------------------------
# Fix C: contemporaneous-score de-bias
# A large same-day move (session_move_vs_prev_close_pct, or return_1d_pct on a
# complete EOD bar) transiently inflates effective_intent_score / next_week_score /
# z_score. These helpers softly discount the portion of those scores attributable to
# today's pop so a one-day spike cannot masquerade as durable strength. Tunable via
# TITAN_CONTEMP_* env knobs; the discount is a bounded soft shave, never a hard zero.
# ---------------------------------------------------------------------------

_CONTEMP_MOVE_THRESHOLD_PCT = 3.0   # same-day move where the discount starts
_CONTEMP_DISCOUNT_SLOPE = 0.08      # discount fraction added per +1% above threshold
_CONTEMP_MAX_DISCOUNT_FRAC = 0.5    # hard ceiling on the discount fraction


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
    """Same-day move: prefer the live intraday snapshot, fall back to the EOD 1d move."""
    move = _safe_float(audit.get("session_move_vs_prev_close_pct"))
    if math.isnan(move):
        move = _safe_float(audit.get("return_1d_pct"))
    return move


def _contemporaneous_discount_factor(audit: dict[str, Any]) -> tuple[float, float]:
    """Return (same_day_move_pct, discount_fraction in [0, max]) for an upside pop."""
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


def _apply_contemporaneous_dampener(audit: dict[str, Any]) -> None:
    """Shave the same-day-pop contribution from effective_intent_score and z_score.

    Mutates the audit in place (these fields are persisted) and records a
    ``contemporaneous_discount`` breakdown. ``next_day_score``/``next_week_score`` are
    de-biased downstream: they read the now-discounted effective_intent_score and the
    matching ret1d discount inside ``_predictive_scores``.
    """
    move, frac = _contemporaneous_discount_factor(audit)
    if frac <= 0.0:
        return
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    discount: dict[str, Any] = {"same_day_move_pct": round(move, 4), "discount_fraction": round(frac, 4)}
    # Only the above-neutral (>50) portion of intent is treated as "earned" and thus
    # partially attributable to today's pop; the baseline is left intact.
    if not math.isnan(eff) and eff > 50.0:
        new_eff = 50.0 + (eff - 50.0) * (1.0 - frac)
        discount["effective_intent_score_before"] = round(eff, 2)
        discount["effective_intent_score_after"] = round(new_eff, 2)
        audit["effective_intent_score"] = round(new_eff, 2)
    # Only a positive (upside) z is shaved; downside-z risk is never softened.
    if not math.isnan(z) and z > 0.0:
        new_z = z * (1.0 - frac)
        discount["z_score_before"] = round(z, 4)
        discount["z_score_after"] = round(new_z, 4)
        audit["z_score"] = new_z
    audit["contemporaneous_discount"] = discount


def _predictive_scores(audit: dict[str, Any]) -> tuple[float, float, dict[str, Any]]:
    from prediction_engine import predictive_scores

    return predictive_scores(audit)


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


def _attach_prior_action_signals(
    cfg: TitanConfig,
    sector_key: str,
    ok_results: list[dict[str, Any]],
) -> None:
    """Wire prior-session labels/risk for v2 hysteresis and recovery de-escalation."""
    if not ok_results or not sector_key:
        return
    try:
        from analysis_store import _fetch_symbol_history_by_sector
    except ImportError:
        return
    history = _fetch_symbol_history_by_sector(cfg, sector=sector_key, lookback_days=21)
    if not history:
        return
    for r in ok_results:
        audit = r.get("audit")
        if not isinstance(audit, dict):
            continue
        sym = str(r.get("symbol") or audit.get("symbol") or "").strip().upper()
        if not sym:
            continue
        rows = history.get(sym) or []
        if not rows:
            continue
        latest = rows[0] if isinstance(rows[0], dict) else {}
        prior = str(latest.get("action_signal") or "").strip().lower()
        if prior:
            audit["prev_action_signal"] = prior
        if len(rows) > 1 and isinstance(rows[1], dict):
            prev_prev = str(rows[1].get("action_signal") or "").strip().lower()
            if prev_prev:
                audit["prev_prev_action_signal"] = prev_prev
        try:
            from signal_v2 import compute_indicator_trajectory, compute_prior_session_streaks

            streaks = compute_prior_session_streaks(rows)
            audit["prior_constructive_streak"] = streaks["prior_constructive_streak"]
            audit["prior_fail_streak"] = streaks["prior_fail_streak"]
            audit["indicator_trajectory"] = compute_indicator_trajectory(
                rows,
                current_audit=audit,
            )
        except ImportError:
            pass
        tape = latest.get("tape_extras")
        if isinstance(tape, dict):
            prev_risk = tape.get("sell_signal_risk_score", tape.get("risk_net"))
            if prev_risk is not None:
                audit["prev_risk_net"] = prev_risk
            prev_mults = tape.get("adx_regime_mults")
            if isinstance(prev_mults, dict):
                audit["prev_adx_regime_mults"] = prev_mults
            prev_regime = tape.get("market_regime")
            if isinstance(prev_regime, dict):
                audit["prev_market_regime"] = prev_regime
            elif tape.get("market_regime_label"):
                audit["prev_market_regime"] = {
                    "regime": tape.get("market_regime_label"),
                    "raw_regime": tape.get("market_regime_raw"),
                    "streak": tape.get("market_regime_streak", 0),
                }


def _refresh_symbol_scoring_outputs(audit: dict[str, Any]) -> None:
    try:
        from market_regime import apply_regime_to_audit

        apply_regime_to_audit(audit)
    except ImportError:
        pass
    if isinstance(audit.get("factor_scores"), dict):
        rollups = audit.get("_sector_rotation_rollups")
        sector_rollups = rollups if isinstance(rollups, list) else None
        _populate_factor_scores(audit, sector_rollups=sector_rollups)
    try:
        from titan_fusion import apply_fusion_to_audit

        apply_fusion_to_audit(audit)
    except ImportError:
        pass
    _apply_contemporaneous_dampener(audit)  # Fix C: de-bias same-day pop before scoring
    next_day_score, next_week_score, prediction_breakdown = _predictive_scores(audit)
    audit["next_day_score"] = next_day_score
    audit["next_week_score"] = next_week_score
    audit["prediction_breakdown"] = prediction_breakdown
    audit["hypothesis_support"] = _hypothesis_support_tag(audit)
    # Drop stale labels so Tier-2 options corroborators do not read a prior pass.
    audit.pop("sell_signal", None)
    audit.pop("action_signal", None)
    action_signal, sell_risk_score, sell_reasons = _derive_sell_signal(audit)
    audit["sell_signal"] = action_signal
    audit["action_signal"] = action_signal
    audit["sell_signal_risk_score"] = sell_risk_score
    audit["sell_signal_reasons"] = sell_reasons
    try:
        from probability_calibration import apply_probability_calibration

        apply_probability_calibration(audit)
    except ImportError:
        pass


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
            # Fallback: news_feed is often empty at feature-run time (per-symbol snapshots
            # are refreshed by a separate job that runs AFTER the sector run). Reuse the
            # latest persisted symbol_news_snapshot so news_count / sentiment are not 0.
            snap = None
            try:
                from news_store import _load_latest_symbol_snapshot

                snap = _load_latest_symbol_snapshot(cfg, inst.symbol)
            except Exception:  # noqa: BLE001 - snapshot fallback is best-effort
                snap = None
            if isinstance(snap, dict) and int(snap.get("news_count") or 0) > 0:
                audit["recent_news"] = snap.get("recent_news_items") or []
                audit["news_count"] = int(snap.get("news_count") or 0)
                audit["news_sentiment_aggregate"] = str(snap.get("aggregate_sentiment") or "neutral")
                audit["news_sentiment_score"] = float(snap.get("aggregate_score") or 0.0)
                trend_val = snap.get("sentiment_trend")
                audit["news_sentiment_trend"] = trend_val if trend_val is not None else "n/a"
                audit["news_source"] = "symbol_news_snapshot_fallback"
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
    from fundamental_engine import assess_fundamental_strength

    return assess_fundamental_strength(cfg, inst)


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


def _news_feed_rows_to_correlator_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map news_feed rows to the item shape expected by correlate_stock_news_with_macro."""
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "summary": str(row.get("summary") or "").strip(),
                "source": str(row.get("source") or "news_feed").strip() or "news_feed",
                "url": str(row.get("url") or "").strip(),
                "published_at": str(row.get("published_at") or "").strip(),
            }
        )
    return items


def _resolve_stock_news_for_symbol(
    cfg: TitanConfig,
    *,
    symbol: str,
    exchange: str,
    allow_live_fetch: bool,
    fetch_stock_news_for_symbol: Any,
) -> dict[str, Any]:
    """Prefer cached news_feed rows; fall back to live Google RSS when allowed."""
    sym = str(symbol).strip().upper()
    ex = str(exchange).strip().upper()
    try:
        from news_store import get_recent_news_for_symbol
    except ImportError as exc:
        logger.warning("news_store unavailable for %s (%s): %s", sym, ex, exc)
        get_recent_news_for_symbol = None  # type: ignore[misc, assignment]

    if get_recent_news_for_symbol is not None:
        try:
            lookback_hours = int(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", 36))
            fetch_limit = int(os.environ.get("TITAN_NEWS_FETCH_LIMIT", 40))
            cached_rows = get_recent_news_for_symbol(
                cfg,
                sym,
                ex,
                lookback_hours=lookback_hours,
                limit=fetch_limit,
            )
            cached_items = _news_feed_rows_to_correlator_items(cached_rows)
            if cached_items:
                return {
                    "symbol": sym,
                    "exchange": ex,
                    "items": cached_items,
                    "query_used": "",
                    "alias_used": "",
                    "fallback_used": False,
                    "error": "",
                    "data_source": "news_feed_cache",
                }
        except Exception as exc:
            logger.warning("Cached news read failed for %s (%s): %s", sym, ex, exc)

    if not allow_live_fetch:
        return {
            "symbol": sym,
            "exchange": ex,
            "items": [],
            "query_used": "",
            "alias_used": "",
            "fallback_used": False,
            "error": "cache_empty_live_skipped",
            "data_source": "none",
        }

    if not callable(fetch_stock_news_for_symbol):
        return {
            "symbol": sym,
            "exchange": ex,
            "items": [],
            "query_used": "",
            "alias_used": "",
            "fallback_used": False,
            "error": "helper_unavailable",
            "data_source": "none",
        }
    try:
        live = fetch_stock_news_for_symbol(cfg, symbol=sym, exchange=ex)
        if isinstance(live, dict):
            live = {**live, "data_source": "google_rss_live"}
            return live
    except Exception as exc:
        return {
            "symbol": sym,
            "exchange": ex,
            "items": [],
            "query_used": "",
            "alias_used": "",
            "fallback_used": False,
            "error": f"unexpected:{exc}",
            "data_source": "none",
        }
    return {
        "symbol": sym,
        "exchange": ex,
        "items": [],
        "query_used": "",
        "alias_used": "",
        "fallback_used": False,
        "error": "unavailable",
        "data_source": "none",
    }


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
    coverage_top_n = _stock_news_coverage_top_n()
    live_fetch_pairs = set(sorted(coverage_pairs)[:coverage_top_n])
    for symbol, exchange in coverage_pairs:
        if not symbol or exchange not in ("NSE", "BSE"):
            continue
        allow_live = (symbol, exchange) in live_fetch_pairs
        stock_news_by_symbol[(symbol, exchange)] = _resolve_stock_news_for_symbol(
            cfg,
            symbol=symbol,
            exchange=exchange,
            allow_live_fetch=allow_live,
            fetch_stock_news_for_symbol=fetch_stock_news_for_symbol,
        )
    if not callable(fetch_stock_news_for_symbol):
        logger.warning(
            "News correlation live Google RSS helper unavailable; cache-only for %s symbols",
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
        stock_aliases = stock_news_meta.get("aliases")
        stock_aliases = stock_aliases if isinstance(stock_aliases, list) else []
        stock_fetch_error = str(stock_news_meta.get("error") or "").strip()
        data_source = str(stock_news_meta.get("data_source") or "").strip()
        if (symbol, exchange) not in stock_news_by_symbol:
            coverage_status = "not_covered"
        elif stock_news_items:
            coverage_status = "cached" if data_source == "news_feed_cache" else "fetched"
        elif stock_fetch_error == "cache_empty_live_skipped":
            coverage_status = "empty:cache_miss_live_skipped"
        elif stock_fetch_error == "helper_unavailable":
            coverage_status = "helper_unavailable"
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
                    aliases=stock_aliases,
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
                "filtered_count": int(stock_news_meta.get("filtered_count") or corr.get("filtered_count") or 0),
                "rejection_samples": (
                    stock_news_meta.get("rejection_samples")
                    if isinstance(stock_news_meta.get("rejection_samples"), list)
                    else (corr.get("rejection_samples") if isinstance(corr.get("rejection_samples"), list) else [])
                ),
                "relevance_top_score": stock_news_meta.get("relevance_top_score") or corr.get("relevance_top_score"),
                "nse_errors": (
                    stock_news_meta.get("nse_errors")
                    if isinstance(stock_news_meta.get("nse_errors"), list)
                    else []
                ),
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


def _apply_sector_rotation_scores(
    cfg: TitanConfig,
    sector_id: str,
    ok_results: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Stamp cross-sector rollups for sector_rotation when available."""
    audits = [r["audit"] for r in ok_results if isinstance(r.get("audit"), dict)]
    if not audits:
        return None
    try:
        from analysis_store import build_rotation_sector_rollups

        rollups = build_rotation_sector_rollups(
            cfg,
            current_sector=sector_id,
            current_audits=audits,
        )
    except ImportError:
        return None
    if len(rollups) < 2:
        return rollups
    for r in ok_results:
        audit = r.get("audit")
        if isinstance(audit, dict):
            audit["_sector_rotation_rollups"] = rollups
    return rollups


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
        assign_percentile("return_21d_pct", "sector_pctile_return_21d_pct")
        assign_percentile("return_63d_pct", "sector_pctile_return_63d_pct")
        assign_percentile("return_126d_pct", "sector_pctile_return_126d_pct")
        assign_percentile(
            "rel_return_20d_vs_nifty_pct", "sector_relative_strength_pctile"
        )
        assign_percentile("median_notional_inr_20d", "sector_pctile_median_notional_20d")
        # Corroborating-only sector percentiles for the v2 money-flow / over-extension
        # gates (persisted into tape_extras so backtests can reproduce the C/C-8 layers).
        assign_percentile("ema200_stretch_atr", "sector_pctile_ema200_stretch")
        assign_percentile("cmf_20", "sector_pctile_cmf_20")
        assign_percentile("adx_14", "sector_pctile_adx_14")

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
            peer_pctile_raw = (os.environ.get("TITAN_THIN_LIQUIDITY_PEER_PCTILE") or "").strip()
            try:
                peer_pctile = float(peer_pctile_raw) if peer_pctile_raw else 12.0
            except ValueError:
                peer_pctile = 12.0
            thin_peer = not math.isnan(sp) and sp <= peer_pctile
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


def _score_risk_factor(audit: dict[str, Any]) -> dict[str, Any]:
    """Defensive risk proxies — does not use post-signal risk_net."""
    score = 100.0
    penalties = 0
    reasons: list[str] = []
    if audit.get("trap_exit_proxy"):
        score -= 25.0
        penalties += 1
        reasons.append("trap_exit_proxy")
    if audit.get("high_volume_down_day_proxy"):
        score -= 15.0
        penalties += 1
        reasons.append("high_volume_down_day_proxy")
    if audit.get("event_risk_soon"):
        score -= 10.0
        penalties += 1
        reasons.append("event_risk_soon")
    atr = _safe_float(audit.get("atr_14_pct"))
    med_atr = _safe_float(audit.get("sector_median_atr_14_pct"))
    if not math.isnan(atr) and not math.isnan(med_atr) and med_atr > 0 and atr > med_atr * 1.5:
        score -= 10.0
        penalties += 1
        reasons.append("elevated_atr_vs_sector")
    if audit.get("history_lt_200_sessions"):
        score -= 10.0
        penalties += 1
        reasons.append("history_lt_200_sessions")
    if audit.get("liquidity_thin_proxy"):
        score -= 15.0
        penalties += 1
        reasons.append("liquidity_thin_proxy")
    score = max(0.0, min(100.0, score))
    confidence = max(0.3, 1.0 - 0.1 * penalties)
    return {
        "score": round(score, 2),
        "confidence": round(confidence, 3),
        "reasons": reasons if reasons else ["clean risk profile"],
        "metadata": {"penalties_applied": penalties},
        "available": True,
    }


def _sector_strength_factor(
    audit: dict[str, Any],
    *,
    sector_rollups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rollups = sector_rollups
    if rollups is None:
        raw = audit.get("_sector_rotation_rollups")
        if isinstance(raw, list):
            rollups = raw
    sector_key = str(audit.get("sector") or audit.get("sector_key") or "").strip().lower()
    if rollups and sector_key:
        try:
            from sector_rotation import score_sector_rotation

            rotation = score_sector_rotation(rollups, sector_key)
            if rotation.get("available"):
                audit["sector_rotation"] = rotation
                return rotation
        except ImportError:
            pass

    pctile = _safe_float(audit.get("sector_relative_strength_pctile"))
    if math.isnan(pctile):
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["sector_relative_strength_pctile missing"],
            "metadata": {},
            "available": False,
        }
    return {
        "score": round(max(0.0, min(100.0, pctile)), 2),
        "confidence": 0.75,
        "reasons": ["sector_relative_strength_pctile"],
        "metadata": {},
        "available": True,
    }


def _populate_factor_scores(
    audit: dict[str, Any],
    *,
    fundamental: dict[str, Any] | None = None,
    sector_rollups: list[dict[str, Any]] | None = None,
) -> None:
    """Populate audit factor_scores for titan_fusion (lazy imports avoid circular deps)."""
    from institutional_flow import score_institutional_flow
    from market_regime import score_market_regime_context
    from relative_strength import score_relative_strength_from_audit

    tech = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    technical: dict[str, Any]
    if math.isnan(tech):
        technical = {
            "score": None,
            "confidence": 0.0,
            "reasons": ["technical score missing"],
            "metadata": {},
            "available": False,
        }
    else:
        technical = {
            "score": round(max(0.0, min(100.0, tech)), 2),
            "confidence": 0.85,
            "reasons": ["effective_intent_score"],
            "metadata": {"source": "effective_intent_score"},
            "available": True,
        }

    flow = score_institutional_flow(audit)
    audit["institutional_flow"] = {
        "available": flow.get("available", False),
        "score": flow.get("score"),
        "confidence": flow.get("confidence"),
        "reasons": flow.get("reasons"),
        "source": "cmf_obv_audit",
        "factor": flow,
    }

    fund_factor = None
    if isinstance(fundamental, dict) and isinstance(fundamental.get("factor"), dict):
        fund_factor = fundamental["factor"]
    elif audit.get("fundamental_score") is not None or (fundamental and fundamental.get("score") is not None):
        score_val = fundamental.get("score") if fundamental else audit.get("fundamental_score")
        fund_factor = {
            "score": float(score_val) if score_val is not None else None,
            "confidence": 0.7,
            "reasons": list((fundamental or {}).get("reasons") or audit.get("fundamental_reasons") or []),
            "metadata": {"status": (fundamental or {}).get("status", audit.get("fundamental_status"))},
            "available": score_val is not None,
        }
    else:
        fund_factor = {
            "score": None,
            "confidence": 0.0,
            "reasons": ["fundamental_score missing"],
            "metadata": {},
            "available": False,
        }

    sector_strength = _sector_strength_factor(audit, sector_rollups=sector_rollups)

    audit["factor_scores"] = {
        "technical": technical,
        "relative_strength": score_relative_strength_from_audit(audit),
        "institutional_flow": flow,
        "fundamentals": fund_factor,
        "market_regime": score_market_regime_context(audit),
        "sector_strength": sector_strength,
        "risk": _score_risk_factor(audit),
    }


def build_equity_live_audit(
    cfg: TitanConfig,
    breeze: Any,
    inst: SectorInstrument,
    *,
    sector_id: str,
    lookback_calendar_days: int | None = None,
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

    from breeze_client import fetch_equity_data, fetch_equity_quote, volume_participation_ratio
    from titan_engine import (
        calculate_adx,
        calculate_atr,
        calculate_atr_ratio,
        calculate_breakout_20d_distances_pct,
        calculate_cmf,
        calculate_ema,
        calculate_equity_technical_score,
        calculate_latest_di,
        calculate_obv_ema,
        calculate_obv_latest,
        calculate_obv_slope,
        calculate_obv_trend_confirm,
        calculate_rsi,
    )

    if lookback_calendar_days is None:
        lookback_calendar_days = max(
            60,
            int(os.environ.get("TITAN_EMA200_LOOKBACK_CALENDAR_DAYS", "400") or 400),
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
            "return_21d_pct": float("nan"),
            "return_63d_pct": float("nan"),
            "return_126d_pct": float("nan"),
            "median_notional_inr_20d": float("nan"),
            "rel_return_5d_vs_nifty_pct": float("nan"),
            "rel_return_10d_vs_nifty_pct": float("nan"),
            "rel_return_20d_vs_nifty_pct": float("nan"),
            "extreme_price_move_proxy": False,
            "ema200_stretch_atr": float("nan"),
            "gap_down_proxy": False,
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
            "obv_latest": float("nan"),
            "obv_ema_20": float("nan"),
            "obv_trend_confirm": None,
            "rsi_14": float("nan"),
        }
        return skip, ""
    metrics_df, ohlc_meta = _prepare_ohlc_for_metrics(df)
    df = metrics_df
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
    ret21d = pct_return_n_sessions_back(series_non_na, 21)
    ret63d = pct_return_n_sessions_back(series_non_na, 63)
    ret126d = pct_return_n_sessions_back(series_non_na, 126)
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
    obv_latest = calculate_obv_latest(df)
    obv_ema_20 = calculate_obv_ema(df, span=20)
    obv_trend_confirm = calculate_obv_trend_confirm(df, span=20)
    rsi_14 = calculate_rsi(series, period=14)
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
    from options_context import build_options_audit_fields, is_fno_symbol

    option_chain_not_fno = not is_fno_symbol(inst.symbol)
    option_chain_fetch_attempted = False
    opt_audit_defaults = build_options_audit_fields(
        {"option_chain_unavailable": True},
        spot=close_last,
    )
    if is_fno_symbol(inst.symbol):
        option_chain_fetch_attempted = True
        try:
            from breeze_client import fetch_option_metrics_with_expiry_fallback

            opt_raw = fetch_option_metrics_with_expiry_fallback(
                breeze,
                inst.symbol,
                max_expiry_tries=4,
            )
            opt_audit_defaults = build_options_audit_fields(opt_raw, spot=close_last)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Option chain unavailable for %s (%s): %s",
                inst.symbol,
                inst.exchange,
                exc,
            )
            opt_audit_defaults["option_chain_unavailable_reason"] = str(exc)
    pcr = opt_audit_defaults.get("pcr", float("nan"))
    intent = calculate_equity_technical_score(z, vpr_for_scoring)
    high_volume_down_day_proxy = (
        not math.isnan(ret1d) and ret1d < 0.0 and not math.isnan(vpr_raw) and vpr_raw >= 1.5
    )
    trap_exit_proxy = (
        not math.isnan(ret1d) and ret1d > 0.0 and not math.isnan(vpr_raw) and vpr_raw <= 0.5
    )
    # Signed, volatility-relative distance from EMA200 (positive = extended above).
    # = ema_200_distance_pct / atr_14_pct; NaN-safe (guards zero/NaN ATR). Trailing
    # inputs only (no lookahead). Future over-extension input; unused by scoring today.
    ema200_stretch_atr = (
        ema_distance_pct / atr_14_pct
        if (
            not math.isnan(ema_distance_pct)
            and not math.isnan(atr_14_pct)
            and atr_14_pct != 0.0
        )
        else float("nan")
    )
    stretch_fields: dict[str, float] = {}
    try:
        from stretch_engine import compute_stretch_metrics

        stretch_fields = compute_stretch_metrics(df)
    except ImportError:
        pass
    # Gap-down proxy: latest session opens materially below the prior close.
    # Threshold is a tunable default (open <= -1.5% vs prior close). Requires a usable
    # 'open' column; otherwise stays False (no fabricated data). Trailing data only.
    gap_down_proxy = False
    if "open" in df.columns and not math.isnan(close_prev) and close_prev != 0.0:
        open_vals = pd.to_numeric(df["open"], errors="coerce")
        open_last = float(open_vals.iloc[-1]) if len(open_vals) else float("nan")
        if not math.isnan(open_last):
            gap_down_proxy = bool(((open_last / close_prev) - 1.0) * 100.0 <= -1.5)
    event_info = _event_flags_for_symbol(inst.symbol, event_snapshot)
    session_move_pct = float("nan")
    session_cmf_20 = float("nan")
    session_volume_participation_ratio = float("nan")
    price_snapshot_ts: str | None = None
    sorted_df = ohlc_meta.get("sorted_df")
    if ohlc_meta.get("session_open"):
        try:
            quote = fetch_equity_quote(
                cfg,
                inst.symbol,
                exchange_used or inst.exchange,
                breeze=breeze,
            )
            if quote:
                session_move_pct, price_snapshot_ts = _session_move_from_quote(quote)
                if ohlc_meta.get("ohlc_bar_incomplete") and sorted_df is not None:
                    session_cmf_20 = _session_cmf_20_from_ohlc(sorted_df, quote)
                    session_volume_participation_ratio = _session_volume_participation_ratio(
                        sorted_df,
                        ohlc_meta.get("metrics_df", df),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Live quote unavailable for %s (%s): %s",
                inst.symbol,
                inst.exchange,
                exc,
            )

    audit: dict[str, Any] = {
        "benchmark": "equity",
        "sector_mode": True,
        "sector": sector_id,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "exchange_used": exchange_used or inst.exchange,
        "exchange_fallback_used": fallback_used,
        "ohlc_bar_as_of_date": ohlc_meta.get("ohlc_bar_as_of_date"),
        "ohlc_bar_incomplete": bool(ohlc_meta.get("ohlc_bar_incomplete")),
        "session_move_vs_prev_close_pct": session_move_pct,
        "session_cmf_20": session_cmf_20,
        "session_volume_participation_ratio": session_volume_participation_ratio,
        "price_snapshot_ts": price_snapshot_ts,
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
        "return_21d_pct": ret21d,
        "return_63d_pct": ret63d,
        "return_126d_pct": ret126d,
        "median_notional_inr_20d": med_notional,
        "extreme_price_move_proxy": extreme_move,
        "rel_return_5d_vs_nifty_pct": rel_map.get("rel_return_5d_vs_nifty_pct", float("nan")),
        "rel_return_10d_vs_nifty_pct": rel_map.get("rel_return_10d_vs_nifty_pct", float("nan")),
        "rel_return_20d_vs_nifty_pct": rel_map.get("rel_return_20d_vs_nifty_pct", float("nan")),
        "ema_200": ema_200,
        "ema_200_distance_pct": ema_distance_pct,
        "ema200_stretch_atr": ema200_stretch_atr,
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
        "obv_latest": obv_latest,
        "obv_ema_20": obv_ema_20,
        "obv_trend_confirm": obv_trend_confirm,
        "rsi_14": rsi_14,
        "atr_break_multiple": atr_break_multiple,
        "structural_break_proxy": (
            not math.isnan(atr_break_multiple) and atr_break_multiple >= 1.5
        ),
        "high_volume_down_day_proxy": high_volume_down_day_proxy,
        "panic_absorption_proxy": high_volume_down_day_proxy,
        "trap_exit_proxy": trap_exit_proxy,
        "gap_down_proxy": gap_down_proxy,
        **event_info,
        "history_lt_200_sessions": len(series_non_na) < 200,
        "pcr": pcr,
        "put_oi": opt_audit_defaults.get("put_oi", 0.0),
        "call_oi": opt_audit_defaults.get("call_oi", 0.0),
        "put_oi_wall_strike": opt_audit_defaults.get("put_oi_wall_strike", float("nan")),
        "call_oi_wall_strike": opt_audit_defaults.get("call_oi_wall_strike", float("nan")),
        "spot_vs_put_wall_pct": opt_audit_defaults.get("spot_vs_put_wall_pct", float("nan")),
        "spot_vs_call_wall_pct": opt_audit_defaults.get("spot_vs_call_wall_pct", float("nan")),
        "oi_wall": opt_audit_defaults.get(
            "oi_wall",
            {"strike": float("nan"), "oi": float("nan")},
        ),
        "option_expiry": opt_audit_defaults.get("option_expiry"),
        "options_expiry": opt_audit_defaults.get("options_expiry"),
        "option_chain_fallback_used": opt_audit_defaults.get("option_chain_fallback_used"),
        "option_chain_expiry_try_index": opt_audit_defaults.get("option_chain_expiry_try_index"),
        "option_chain_expiry_tries": opt_audit_defaults.get("option_chain_expiry_tries"),
        "intent_score": intent,
        "effective_intent_score": intent,
        "equity_technical_score": intent,
        "rows": len(df),
        "option_chain_unavailable": bool(opt_audit_defaults.get("option_chain_unavailable", True)),
        "option_chain_unavailable_reason": opt_audit_defaults.get("option_chain_unavailable_reason"),
        "option_chain_not_fno": option_chain_not_fno,
        "option_chain_fetch_attempted": option_chain_fetch_attempted,
    }
    macro_ctx = getattr(_THREAD_LOCAL, "sector_macro_context", None)
    if isinstance(macro_ctx, dict) and isinstance(macro_ctx.get("institutional"), dict):
        audit["market_institutional_flow"] = dict(macro_ctx["institutional"])
    try:
        from institutional_flow import enrich_audit_institutional_data

        enrich_audit_institutional_data(
            audit,
            cfg,
            inst,
            as_of_date=str(audit.get("ohlc_bar_as_of_date") or "").strip() or None,
        )
    except ImportError:
        audit["institutional_flow"] = {"available": False, "source": None}
    fundamental = _assess_fundamental_strength(cfg, inst)
    audit["fundamental_status"] = fundamental.get("status", "unavailable")
    audit["fundamental_score"] = fundamental.get("score")
    audit["fundamental_reasons"] = fundamental.get("reasons", [])
    if isinstance(macro_ctx, dict):
        from breadth_engine import apply_macro_context_to_audit

        apply_macro_context_to_audit(audit, macro_ctx)
    _populate_factor_scores(audit, fundamental=fundamental)
    if stretch_fields:
        audit.update(stretch_fields)
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
    ensure_utf8_stdio()
    from email_notify import send_success_post_email
    from breeze_client import create_breeze_session
    from sector_registry import resolve_sector_key

    raw_sector_id = sector_id
    sector_id = resolve_sector_key(sector_id)
    if sector_id != raw_sector_id.strip().lower():
        logger.info("Resolved sector alias %r -> %r", raw_sector_id, sector_id)

    cfg = load_config()
    # Preflight Breeze auth once to fail fast on expired tokens.
    # Without this, each worker thread would emit the same auth stacktrace.
    breeze = create_breeze_session(cfg)
    import pandas as pd

    from breeze_client import fetch_nifty_data, fetch_option_metrics_with_expiry_fallback
    from options_context import build_sector_options_digest, sector_options_underlying

    sector_options_ctx: dict[str, Any] = {"sector_option_chain_unavailable": True}
    macro_ctx: dict[str, Any] = {}
    try:
        _nifty_df = fetch_nifty_data(cfg, breeze=breeze, lookback_calendar_days=210)
        _THREAD_LOCAL.sector_benchmark_ohlc = _nifty_df if not _nifty_df.empty else None
    except Exception as ex:
        logger.warning("NIFTY benchmark prefetch skipped: %s", ex)
        _THREAD_LOCAL.sector_benchmark_ohlc = None
        _nifty_df = pd.DataFrame()

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

    try:
        from breadth_engine import BREADTH_PANEL_MAX_SYMBOLS, compute_market_breadth, prefetch_breadth_panel, stamp_macro_context

        breadth_panel = prefetch_breadth_panel(
            cfg,
            breeze,
            instruments,
            max_symbols=BREADTH_PANEL_MAX_SYMBOLS,
        )
        breadth_metrics = compute_market_breadth(breadth_panel) if breadth_panel else {}
        macro_ctx = stamp_macro_context(
            nifty_df=_nifty_df if not _nifty_df.empty else None,
            macro_snapshot=macro_snapshot,
            breadth_metrics=breadth_metrics if breadth_metrics.get("n_symbols") else None,
        )
        _THREAD_LOCAL.sector_macro_context = macro_ctx
    except ImportError:
        try:
            from breadth_engine import stamp_macro_context

            macro_ctx = stamp_macro_context(
                nifty_df=_nifty_df if not _nifty_df.empty else None,
                macro_snapshot=macro_snapshot,
            )
            _THREAD_LOCAL.sector_macro_context = macro_ctx
        except ImportError:
            _THREAD_LOCAL.sector_macro_context = {}
    except Exception as ex:
        logger.warning("Breadth panel prefetch skipped for %s: %s", sector_id, ex)
        try:
            from breadth_engine import stamp_macro_context

            macro_ctx = stamp_macro_context(
                nifty_df=_nifty_df if not _nifty_df.empty else None,
                macro_snapshot=macro_snapshot,
            )
            _THREAD_LOCAL.sector_macro_context = macro_ctx
        except ImportError:
            _THREAD_LOCAL.sector_macro_context = {}

    if digest:
        try:
            underlying = sector_options_underlying(sector_id)
            opt = fetch_option_metrics_with_expiry_fallback(breeze, underlying, max_expiry_tries=6)
            nifty_spot = float("nan")
            if not _nifty_df.empty and "close" in _nifty_df.columns:
                nifty_spot = float(
                    pd.to_numeric(_nifty_df["close"], errors="coerce").dropna().iloc[-1]
                )
            sector_options_ctx = build_sector_options_digest(
                opt,
                spot=nifty_spot,
                sector_id=sector_id,
            )
        except Exception as ex:
            logger.warning("Sector options context fetch skipped for %s: %s", sector_id, ex)

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
    if hasattr(_THREAD_LOCAL, "sector_macro_context"):
        delattr(_THREAD_LOCAL, "sector_macro_context")

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
        _bridge_priority_shadow_context(cfg, sector_id, ok_results)
        red_ratio, cluster_downgrades = _apply_cluster_guardrails(ok_results)
        event_adjustments = _apply_event_guardrails(ok_results)
        macro_applied, macro_reason = _apply_macro_guardrails(ok_results, macro_snapshot)
        _apply_sector_cross_section(ok_results, score_percentiles=False)
        _apply_sector_rotation_scores(cfg, sector_id, ok_results)
        _attach_prior_action_signals(cfg, sector_id, ok_results)
        for r in ok_results:
            if isinstance(r.get("audit"), dict):
                r["audit"]["sector_options_context"] = sector_options_ctx
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
        if _pm_macro_email_enabled(sector_id):
            pm_lines = _build_precious_metals_macro_digest_lines(
                as_of_date=_digest_eod_as_of_date(results),
            )
            if pm_lines:
                lines.extend([""] + pm_lines + [""])
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
        sector_opt_lines = _format_sector_options_context_block(sector_options_ctx)
        if sector_opt_lines:
            lines.extend(["", "--- Sector options context ---", *sector_opt_lines])
        lines.append("--- Action summary ---")
        from action_signals import normalize_action_signal

        action_counts = {"buy": 0, "accumulate": 0, "hold": 0, "trim": 0, "exit-risk": 0}
        for r in ok_results:
            sig = normalize_action_signal((r.get("audit") or {}).get("sell_signal"))
            action_counts[sig] += 1
        lines.extend(
            [
                f"BUY: {action_counts['buy']} | ACCUMULATE: {action_counts['accumulate']} | "
                f"HOLD: {action_counts['hold']} | TRIM: {action_counts['trim']} | "
                f"EXIT RISK: {action_counts['exit-risk']}",
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
        _append_investment_report_digest_lines(lines, ok_results)
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
            send_success_post_email(
                digest_text,
                subject_prefix=f"Titan V12.0 sector {sector_id}",
                eod_as_of_date=_digest_eod_as_of_date(results),
            )
        _safe_print(digest_text)
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
        send_success_post_email(
            digest_out,
            subject_prefix=f"Titan V12.0 sector {sector_id}",
            eod_as_of_date=_digest_eod_as_of_date(results),
        )
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
    _safe_print(digest_out)
    return digest_out
