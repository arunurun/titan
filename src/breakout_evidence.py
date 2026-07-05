"""Pure evidence metrics for v7 breakout scanner (no I/O or scanner wiring)."""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence

# Tier keys aligned with breakout_scanner.INDEX_URLS / FILTERS
TIER_SMALL_CAP = "SMALL_CAP_100"
TIER_MICRO_CAP = "MICRO_CAP_250"

# Liquidity gates (INR): small >= 2cr, micro >= 3cr
SMALL_CAP_MIN_MEDIAN_TURNOVER_INR = 20_000_000.0
MICRO_CAP_MIN_MEDIAN_TURNOVER_INR = 30_000_000.0

# Liquidity quality composite weights (user spec)
_LIQ_W_TURNOVER = 0.35
_LIQ_W_DELIVERY = 0.25
_LIQ_W_VOL_CONSISTENCY = 0.20
_LIQ_W_FREE_FLOAT = 0.20

# Participation / flow thresholds by tier (user universe rules)
SMALL_CAP_VPR_MIN = 1.5
SMALL_CAP_CMF_MIN = 0.0
MICRO_CAP_VPR_MIN = 2.0
MICRO_CAP_CMF_MIN = 0.05
MICRO_CAP_DELIVERY_PCT_MIN = 40.0
MICRO_CAP_DELIVERY_AVG_RATIO = 0.8
DELIVERY_ANOMALY_LOOKBACK = 20
RS_BENCHMARK_FLAT_BONUS = 10.0

# Base quality weights (user spec)
_BASE_W_COMPRESSION = 0.40
_BASE_W_TIGHT = 0.30
_BASE_W_PIVOT = 0.30

BASE_ACCUM_LOOKBACK = 30
BASE_ACCUM_MIN_RATIO = 1.05

BreakoutStage = Literal[1, 2, 3]
_NEUTRAL_SUBSCORE = 50.0


def _is_finite(value: float | int | None) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(v) and math.isfinite(v)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalize_tier(tier: str | None) -> str | None:
    if tier is None:
        return None
    key = str(tier).strip().upper()
    if key in (TIER_SMALL_CAP, TIER_MICRO_CAP):
        return key
    lowered = str(tier).lower()
    if "micro" in lowered:
        return TIER_MICRO_CAP
    if "small" in lowered:
        return TIER_SMALL_CAP
    return key


def _turnover_subscore_0_100(turnover_inr: float) -> float:
    """Map median daily turnover INR to 0-100 (2cr ~40, 5cr ~70, 10cr+ ~100)."""
    # Reference: 10 crore median daily turnover = excellent liquidity for this universe.
    ref = 100_000_000.0
    return _clamp(100.0 * turnover_inr / ref, 0.0, 100.0)


def _pct_subscore_0_100(value: float) -> float:
    """Percent-like inputs (0-100 scale) clamped to subscore."""
    return _clamp(float(value), 0.0, 100.0)


def _consolidation_subscore_0_100(consolidation_days: float) -> float:
    """Longer bases score higher; 30+ sessions -> 100."""
    return _clamp(100.0 * float(consolidation_days) / 30.0, 0.0, 100.0)


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return _NEUTRAL_SUBSCORE
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return _NEUTRAL_SUBSCORE
    return sum(v * w for v, w in pairs) / total_w


def liquidity_quality_score(
    turnover_20d: float | None,
    delivery_pct: float | None,
    vol_consistency: float | None,
    free_float_pct: float | None,
) -> float:
    """
    Weighted liquidity quality on 0-100.

    Missing inputs contribute neutral 50 and their weight is excluded from the blend
    (remaining weights renormalized).
    """
    components: list[tuple[float, float]] = []
    if _is_finite(turnover_20d):
        components.append((_turnover_subscore_0_100(float(turnover_20d)), _LIQ_W_TURNOVER))
    if _is_finite(delivery_pct):
        components.append((_pct_subscore_0_100(float(delivery_pct)), _LIQ_W_DELIVERY))
    if _is_finite(vol_consistency):
        components.append((_pct_subscore_0_100(float(vol_consistency)), _LIQ_W_VOL_CONSISTENCY))
    if _is_finite(free_float_pct):
        components.append((_pct_subscore_0_100(float(free_float_pct)), _LIQ_W_FREE_FLOAT))
    if not components:
        return _NEUTRAL_SUBSCORE
    return round(_weighted_mean(components), 2)


def _liquidity_turnover_inr(
    median_turnover_inr: float | None,
    *,
    session_notional_inr: float | None = None,
) -> float | None:
    """Effective turnover for gate: median first, else session Volume(T)*Close(T)."""
    if _is_finite(median_turnover_inr):
        return float(median_turnover_inr)
    if _is_finite(session_notional_inr):
        return float(session_notional_inr)
    return None


def liquidity_gate_fail_reason(
    tier: str | None,
    median_turnover_inr: float | None,
    *,
    session_notional_inr: float | None = None,
) -> str | None:
    """
    Hard liquidity gate by tier (fail-closed).

    Small cap: median daily turnover >= 2 crore INR.
    Micro cap: median daily turnover >= 3 crore INR.
    Missing median falls back to session Volume(T)*Close(T); both missing fails.
    """
    turnover = _liquidity_turnover_inr(
        median_turnover_inr, session_notional_inr=session_notional_inr,
    )
    if turnover is None:
        return "missing_liquidity_data"
    tier_key = _normalize_tier(tier)
    floor = SMALL_CAP_MIN_MEDIAN_TURNOVER_INR
    if tier_key == TIER_MICRO_CAP:
        floor = MICRO_CAP_MIN_MEDIAN_TURNOVER_INR
    if turnover < floor:
        return "pre_filter_liquidity"
    return None


def liquidity_gate_pass(
    tier: str | None,
    median_turnover_inr: float | None,
    *,
    session_notional_inr: float | None = None,
) -> bool:
    """Return True when liquidity gate passes; see liquidity_gate_fail_reason."""
    return liquidity_gate_fail_reason(
        tier, median_turnover_inr, session_notional_inr=session_notional_inr,
    ) is None


def volume_persistence_score(
    volumes: Sequence[float | int | None],
    vol_20_avg: float | None,
    *,
    lookback: int = 10,
    threshold: float = 1.5,
) -> int:
    """
    Count sessions in the last `lookback` with volume > threshold * vol_20_avg.

    Scoring: 0 days -> 0, 1 -> 1, 2 -> 2, 3+ -> 4.
    Returns 0 when volume history or average is unusable.
    """
    if lookback <= 0 or not _is_finite(vol_20_avg) or float(vol_20_avg) <= 0:
        return 0
    if not volumes:
        return 0

    tail = list(volumes)[-lookback:]
    cutoff = float(vol_20_avg) * float(threshold)
    persistent_days = sum(
        1 for v in tail if _is_finite(v) and float(v) > cutoff
    )

    if persistent_days <= 0:
        return 0
    if persistent_days == 1:
        return 1
    if persistent_days == 2:
        return 2
    return 4


def breakout_stage(
    near_52w_high: bool | None,
    base_duration_days: float | None,
    stretch_atr: float | None,
    breakout_age_days: float | None,
) -> BreakoutStage | None:
    """
    Classify breakout lifecycle stage.

    Stage 1: fresh base break (near 52w high, base > 30d, stretch < 2 ATR).
    Stage 2: young trend (breakout age < 20 sessions).
    Stage 3: parabolic extension (stretch > 4 ATR).
    """
    stretch = float(stretch_atr) if _is_finite(stretch_atr) else None

    if (
        near_52w_high is True
        and _is_finite(base_duration_days)
        and float(base_duration_days) > 30.0
        and stretch is not None
        and stretch < 2.0
    ):
        return 1

    if _is_finite(breakout_age_days) and float(breakout_age_days) < 20.0:
        return 2

    if stretch is not None and stretch > 4.0:
        return 3

    return None


def base_accumulation_ratio(
    opens: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    as_of_idx: int,
    *,
    lookback: int = BASE_ACCUM_LOOKBACK,
) -> dict[str, Any]:
    """Up/down volume over [T-lookback .. T-1]: up = close>open, down = close<open."""
    t = as_of_idx
    start = max(0, t - lookback)
    up_vol = 0.0
    down_vol = 0.0
    for i in range(start, t):
        o = float(opens[i])
        c = float(closes[i])
        v = float(volumes[i])
        if c > o:
            up_vol += v
        elif c < o:
            down_vol += v
    ratio: float | None
    if down_vol > 0:
        ratio = round(up_vol / down_vol, 4)
    elif up_vol > 0:
        ratio = None
    else:
        ratio = 1.0
    return {
        "up_volume": round(up_vol, 2),
        "down_volume": round(down_vol, 2),
        "ratio": ratio,
        "lookback_bars": t - start,
        "min_ratio_required": BASE_ACCUM_MIN_RATIO,
    }


def base_accumulation_pass(
    opens: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    as_of_idx: int,
    *,
    lookback: int = BASE_ACCUM_LOOKBACK,
    min_ratio: float = BASE_ACCUM_MIN_RATIO,
) -> tuple[bool, dict[str, Any]]:
    """True when up-day volume >= down-day volume * min_ratio over the base window."""
    stats = base_accumulation_ratio(
        opens, closes, volumes, as_of_idx, lookback=lookback,
    )
    up_vol = float(stats["up_volume"])
    down_vol = float(stats["down_volume"])
    passed = up_vol >= down_vol * min_ratio
    stats["passed"] = passed
    return passed, stats


def base_quality_score(
    consolidation_days: float | None,
    range_contraction_pct: float | None,
    atr_compression: float | None,
    pivot_proximity: float | None,
) -> float:
    """
    Base formation quality on 0-100.

    compression blends ATR compression and range contraction;
    tight_closes from consolidation duration; pivot_proximity direct.
    Missing inputs use neutral 50 with weight exclusion.
    """
    compression_parts: list[tuple[float, float]] = []
    if _is_finite(atr_compression):
        compression_parts.append((_pct_subscore_0_100(float(atr_compression)), 1.0))
    if _is_finite(range_contraction_pct):
        compression_parts.append((_pct_subscore_0_100(float(range_contraction_pct)), 1.0))
    compression = (
        _weighted_mean(compression_parts)
        if compression_parts
        else _NEUTRAL_SUBSCORE
    )

    tight = (
        _consolidation_subscore_0_100(float(consolidation_days))
        if _is_finite(consolidation_days)
        else _NEUTRAL_SUBSCORE
    )
    pivot = (
        _pct_subscore_0_100(float(pivot_proximity))
        if _is_finite(pivot_proximity)
        else _NEUTRAL_SUBSCORE
    )

    pairs: list[tuple[float, float]] = []
    if compression_parts:
        pairs.append((compression, _BASE_W_COMPRESSION))
    if _is_finite(consolidation_days):
        pairs.append((tight, _BASE_W_TIGHT))
    if _is_finite(pivot_proximity):
        pairs.append((pivot, _BASE_W_PIVOT))

    if not pairs:
        return _NEUTRAL_SUBSCORE

    return round(_weighted_mean(pairs), 2)


def micro_cap_stricter_rules(tier: str | None = None) -> dict[str, Any]:
    """
    Return tier-specific participation thresholds (VPR / CMF / delivery).

    Includes liquidity turnover floors used by liquidity_gate_pass.
    """
    tier_key = _normalize_tier(tier)
    if tier_key == TIER_MICRO_CAP:
        return {
            "tier": TIER_MICRO_CAP,
            "median_turnover_inr_min": MICRO_CAP_MIN_MEDIAN_TURNOVER_INR,
            "vpr_min": MICRO_CAP_VPR_MIN,
            "cmf_min": MICRO_CAP_CMF_MIN,
            "delivery_pct_min": MICRO_CAP_DELIVERY_PCT_MIN,
        }
    return {
        "tier": TIER_SMALL_CAP,
        "median_turnover_inr_min": SMALL_CAP_MIN_MEDIAN_TURNOVER_INR,
        "vpr_min": SMALL_CAP_VPR_MIN,
        "cmf_min": SMALL_CAP_CMF_MIN,
        "delivery_pct_min": None,
    }


def delivery_anomaly_required_pct(
    delivery_pct: float | None,
    avg_delivery_pct: float | None,
    *,
    static_floor: float = MICRO_CAP_DELIVERY_PCT_MIN,
    avg_ratio: float = MICRO_CAP_DELIVERY_AVG_RATIO,
) -> float | None:
    """Effective delivery floor: max(static floor, avg[T-20..T-1] × ratio)."""
    floor = float(static_floor)
    if _is_finite(avg_delivery_pct):
        floor = max(floor, float(avg_delivery_pct) * float(avg_ratio))
    return floor


def micro_cap_participation_pass(
    tier: str | None,
    *,
    vpr: float | None = None,
    cmf: float | None = None,
    delivery_pct: float | None = None,
    avg_delivery_pct: float | None = None,
) -> bool | None:
    """
    Check VPR/CMF/delivery against tier thresholds.

    Micro-cap delivery uses day-T delivery vs trailing avg (T-20..T-1) × 0.8
    with the static 40% floor. When day-T delivery is missing, the delivery leg
    is bypassed (caller should set ``PENDING_DELIVERY_DATA`` risk flag).

    Returns None when no participation inputs are available (skip check).
    """
    rules = micro_cap_stricter_rules(tier)
    checks: list[bool] = []

    if _is_finite(vpr):
        checks.append(float(vpr) > float(rules["vpr_min"]))
    if _is_finite(cmf):
        checks.append(float(cmf) > float(rules["cmf_min"]))

    delivery_min = rules.get("delivery_pct_min")
    if delivery_min is not None and _is_finite(delivery_pct):
        required = delivery_anomaly_required_pct(
            delivery_pct, avg_delivery_pct, static_floor=float(delivery_min),
        )
        if required is not None:
            checks.append(float(delivery_pct) >= required)

    if not checks:
        return None
    return all(checks)


def persistence_pass_min(tier: str | None) -> int:
    """Minimum volume persistence score for full PASS by tier."""
    return 2 if _normalize_tier(tier) == TIER_MICRO_CAP else 1


def median_notional_inr(
    closes: Sequence[float],
    volumes: Sequence[float],
    as_of_idx: int,
    *,
    window: int = 20,
) -> float | None:
    """Median daily notional (INR) over T-window..T-1."""
    t = as_of_idx
    start = max(0, t - window)
    notionals = [
        float(closes[i]) * float(volumes[i])
        for i in range(start, t)
        if _is_finite(closes[i]) and _is_finite(volumes[i]) and float(closes[i]) > 0
    ]
    if not notionals:
        return None
    notionals.sort()
    return notionals[len(notionals) // 2]


def volume_consistency_from_series(
    volumes: Sequence[float],
    as_of_idx: int,
    *,
    window: int = 20,
) -> float | None:
    """Coefficient-of-variation consistency mapped to 0-100 subscore input."""
    t = as_of_idx
    start = max(0, t - window)
    seg = [float(v) for v in volumes[start:t] if _is_finite(v)]
    if len(seg) < 5:
        return None
    mean_v = sum(seg) / len(seg)
    if mean_v <= 0:
        return None
    variance = sum((x - mean_v) ** 2 for x in seg) / len(seg)
    cv = math.sqrt(variance) / mean_v
    return _clamp(100.0 * (1.0 - cv), 0.0, 100.0)


def _atr_simple(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> list[float]:
    n = len(close)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(
            float(high[i]) - float(low[i]),
            abs(float(high[i]) - float(close[i - 1])),
            abs(float(low[i]) - float(close[i - 1])),
        )
    atr = [0.0] * n
    if n <= period:
        return atr
    atr[period] = sum(tr[1 : period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _flow_metrics_from_bars(df: dict[str, Any], as_of_idx: int) -> tuple[float | None, float | None]:
    """VPR and CMF from OHLCV bars through signal day T (inclusive)."""
    try:
        import pandas as pd
        from breeze_client import volume_participation_ratio
        from titan_engine import calculate_cmf
    except ImportError:
        return None, None

    n = as_of_idx + 1
    if n < 2:
        return None, None
    ohlc = pd.DataFrame({
        "high": [float(x) for x in df["high"][:n]],
        "low": [float(x) for x in df["low"][:n]],
        "close": [float(x) for x in df["close"][:n]],
        "volume": [float(x) for x in df["volume"][:n]],
    })
    vpr_raw = volume_participation_ratio(ohlc)
    cmf_raw = calculate_cmf(ohlc, window=min(20, n))
    vpr = float(vpr_raw) if _is_finite(vpr_raw) else None
    cmf = float(cmf_raw) if _is_finite(cmf_raw) else None
    return vpr, cmf


def compute_evidence_inputs(
    df: dict[str, Any],
    as_of_idx: int,
    *,
    bhav_turnover_lacs: float | None = None,
) -> dict[str, Any]:
    """Derive bar-level inputs for evidence scoring at signal day T."""
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]
    t = as_of_idx
    n = t + 1

    median_inr = median_notional_inr(closes, volumes, t)
    if _is_finite(bhav_turnover_lacs) and float(bhav_turnover_lacs) > 0:
        median_inr = float(bhav_turnover_lacs) * 100_000.0

    vol_cons = volume_consistency_from_series(volumes, t)

    lookback_high = min(252, n - 1)
    high_window_start = max(0, n - 1 - lookback_high)
    high_52w = max(float(h) for h in highs[high_window_start:n])
    near_52w_high = float(closes[t]) >= high_52w * 0.97 if high_52w > 0 else False

    w20_start = max(0, t - 20)
    w40_start = max(0, t - 40)
    range_20 = max(float(h) for h in highs[w20_start:t]) - min(float(l) for l in lows[w20_start:t])
    range_40 = max(float(h) for h in highs[w40_start:t]) - min(float(l) for l in lows[w40_start:t])
    range_contraction = (
        _clamp(100.0 * (1.0 - (range_20 / range_40)), 0.0, 100.0) if range_40 > 0 else None
    )

    atr_arr = _atr_simple(highs[:n], lows[:n], closes[:n])
    # Base-quality compression: ATR windows strictly before signal day T.
    atr_recent_end = t
    atr_recent_start = max(0, t - 5)
    atr_prior_end = max(0, t - 5)
    atr_prior_start = max(0, t - 15)
    atr_recent = sum(atr_arr[atr_recent_start:atr_recent_end]) / max(
        1, atr_recent_end - atr_recent_start,
    )
    atr_prior = sum(atr_arr[atr_prior_start:atr_prior_end]) / max(
        1, atr_prior_end - atr_prior_start,
    )
    atr_compression = (
        _clamp(100.0 * (1.0 - (atr_recent / atr_prior)), 0.0, 100.0) if atr_prior > 0 else None
    )

    cons_high = max(float(h) for h in highs[max(0, t - 30) : t])
    cons_low = min(float(l) for l in lows[max(0, t - 30) : t])
    cons_range = cons_high - cons_low
    consolidation_days = 0
    for i in range(max(0, t - 40), t):
        if cons_range > 0 and (float(highs[i]) - float(lows[i])) / cons_range <= 0.35:
            consolidation_days += 1

    pivot = cons_high
    pre_breakout_close = float(closes[t - 1]) if t >= 1 else float(closes[t])
    pivot_proximity = (
        _clamp(100.0 * (1.0 - abs(pre_breakout_close - pivot) / pivot), 0.0, 100.0)
        if pivot > 0
        else None
    )

    sma50 = sum(float(c) for c in closes[max(0, t - 49) : t + 1]) / min(50, t + 1)
    atr_t = atr_arr[t] if atr_arr[t] > 0 else 1.0
    stretch_atr = (float(closes[t]) - sma50) / atr_t

    high_20 = max(float(h) for h in highs[max(0, t - 20) : t]) if t > 0 else float(closes[t])
    breakout_age = 0
    for i in range(t - 1, max(0, t - 60), -1):
        if float(closes[i]) >= high_20 * 0.995:
            breakout_age = t - i
            break

    return {
        "median_turnover_inr": median_inr,
        "vol_consistency": vol_cons,
        "near_52w_high": near_52w_high,
        "base_duration_days": consolidation_days,
        "stretch_atr": round(stretch_atr, 4),
        "breakout_age_days": breakout_age,
        "range_contraction_pct": range_contraction,
        "atr_compression": atr_compression,
        "pivot_proximity": pivot_proximity,
        "consolidation_days": consolidation_days,
    }


def return_5d_pct(
    closes: Sequence[float],
    as_of_idx: int,
) -> float | None:
    """Close-to-close % return over five sessions ending at ``as_of_idx``."""
    t = as_of_idx
    if t < 5:
        return None
    c0 = float(closes[t - 5])
    c1 = float(closes[t])
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 4)


def relative_strength_vs_benchmark(
    stock_5d_return: float | None,
    benchmark_5d_return: float | None,
) -> float | None:
    """Stock minus benchmark 5d return (percentage points)."""
    if not _is_finite(stock_5d_return) or not _is_finite(benchmark_5d_return):
        return None
    return round(float(stock_5d_return) - float(benchmark_5d_return), 4)


def relative_strength_rank_subscore(
    rel_return_5d: float | None,
    *,
    benchmark_5d_return: float | None = None,
) -> float:
    """
    Map relative 5d return to 0-100 rank subscore.

    Bonus when the benchmark is flat/negative and the stock is outperforming.
    """
    if not _is_finite(rel_return_5d):
        return _NEUTRAL_SUBSCORE
    rs = _clamp(50.0 + float(rel_return_5d) * 5.0, 0.0, 100.0)
    if (
        _is_finite(benchmark_5d_return)
        and float(benchmark_5d_return) <= 0.0
        and float(rel_return_5d) > 0.0
    ):
        rs = _clamp(rs + RS_BENCHMARK_FLAT_BONUS, 0.0, 100.0)
    return rs


def compute_evidence_metrics(
    df: dict[str, Any],
    as_of_idx: int,
    tier_name: str,
    vol_20_avg: Sequence[float],
    *,
    bhav_turnover_lacs: float | None = None,
    delivery_pct: float | None = None,
    avg_delivery_pct: float | None = None,
    free_float_pct: float | None = None,
) -> dict[str, Any]:
    """Evidence bundle for scanner integration at bar T."""
    volumes = df["volume"]
    t = as_of_idx
    inputs = compute_evidence_inputs(df, as_of_idx, bhav_turnover_lacs=bhav_turnover_lacs)
    vpr, cmf = _flow_metrics_from_bars(df, as_of_idx)
    part_pass = micro_cap_participation_pass(
        tier_name,
        vpr=vpr,
        cmf=cmf,
        delivery_pct=delivery_pct,
        avg_delivery_pct=avg_delivery_pct,
    )
    participation_risk_flag: str | None = None
    tier_key = _normalize_tier(tier_name)
    if tier_key == TIER_MICRO_CAP and not _is_finite(delivery_pct):
        participation_risk_flag = "PENDING_DELIVERY_DATA"

    closes = df["close"]
    median_inr = inputs["median_turnover_inr"]
    session_notional: float | None = None
    if _is_finite(closes[t]) and _is_finite(volumes[t]):
        session_notional = float(closes[t]) * float(volumes[t])
    liq_fail = liquidity_gate_fail_reason(
        tier_name, median_inr, session_notional_inr=session_notional,
    )
    liq_ok = liq_fail is None
    liq_quality = liquidity_quality_score(
        median_inr,
        delivery_pct,
        inputs["vol_consistency"],
        free_float_pct,
    )
    vma = float(vol_20_avg[t - 1]) if t > 0 and t - 1 < len(vol_20_avg) else float(vol_20_avg[-1])
    persist = volume_persistence_score(
        volumes[max(0, t - 10) : t],
        vma,
        lookback=min(10, t),
    )
    stage = breakout_stage(
        inputs["near_52w_high"],
        inputs["base_duration_days"],
        inputs["stretch_atr"],
        inputs["breakout_age_days"],
    )
    base_score = base_quality_score(
        inputs["consolidation_days"],
        inputs["range_contraction_pct"],
        inputs["atr_compression"],
        inputs["pivot_proximity"],
    )

    return {
        "liquidity_gate_pass": liq_ok,
        "liquidity_gate_fail": liq_fail,
        "liquidity_quality": liq_quality,
        "median_turnover_inr": round(float(median_inr), 2) if _is_finite(median_inr) else None,
        "delivery_pct": round(float(delivery_pct), 4) if _is_finite(delivery_pct) else None,
        "free_float_pct": round(float(free_float_pct), 4) if _is_finite(free_float_pct) else None,
        "vpr": round(vpr, 4) if vpr is not None else None,
        "cmf": round(cmf, 4) if cmf is not None else None,
        "micro_participation_pass": part_pass,
        "participation_risk_flag": participation_risk_flag,
        "avg_delivery_pct": (
            round(float(avg_delivery_pct), 4) if _is_finite(avg_delivery_pct) else None
        ),
        "persistence_score": persist,
        "persistence_pass_min": persistence_pass_min(tier_name),
        "breakout_stage": stage,
        "base_score": base_score,
        "stretch_atr": inputs["stretch_atr"],
        "breakout_age_days": inputs["breakout_age_days"],
        "near_52w_high": inputs["near_52w_high"],
    }


RANK_WEIGHTS: dict[str, float] = {
    "breakout": 0.25,
    "sector_lead": 0.20,
    "base": 0.15,
    "vol_persistence": 0.15,
    "acceleration": 0.10,
    "rs": 0.10,
    "risk_penalty": 0.05,
}


def composite_rank_score(
    metrics: dict[str, Any],
    *,
    sector_lead: float | None = None,
) -> float:
    """Weighted composite rank for PASS candidates (0-100)."""
    vol_mult = float(metrics.get("vol_mult") or 0.0)
    pct_change = float(metrics.get("pct_change") or 0.0)
    breakout = _clamp(vol_mult / 7.0 * 50.0 + pct_change / 12.0 * 50.0, 0.0, 100.0)

    sector = float(sector_lead) if _is_finite(sector_lead) else 50.0
    base = float(metrics.get("base_score") or _NEUTRAL_SUBSCORE)
    persist_raw = int(metrics.get("persistence_score") or 0)
    persist = persist_raw / 4.0 * 100.0

    adx_traj = metrics.get("adx_trajectory") or {}
    adx_t1 = adx_traj.get("adx_t1")
    adx_t10 = adx_traj.get("adx_t10")
    if _is_finite(adx_t1) and _is_finite(adx_t10) and float(adx_t10) > 0:
        acceleration = _clamp(
            (float(adx_t1) - float(adx_t10)) / float(adx_t10) * 100.0 + pct_change * 3.0,
            0.0,
            100.0,
        )
    else:
        acceleration = _clamp(pct_change / 10.0 * 100.0, 0.0, 100.0)

    rel_5d = metrics.get("rel_return_5d_vs_benchmark")
    bench_5d = metrics.get("benchmark_5d_return")
    if _is_finite(rel_5d):
        rs = relative_strength_rank_subscore(float(rel_5d), benchmark_5d_return=bench_5d)
    else:
        rsi_val = float(metrics.get("rsi_val") or 50.0)
        rs = _clamp((rsi_val - 30.0) / 40.0 * 100.0, 0.0, 100.0)

    risk = 100.0
    if metrics.get("breakout_stage") == 3:
        risk -= 40.0
    if "power_gap" in (metrics.get("pass_paths") or []):
        risk -= 20.0
    risk = _clamp(risk, 0.0, 100.0)

    return round(
        breakout * RANK_WEIGHTS["breakout"]
        + sector * RANK_WEIGHTS["sector_lead"]
        + base * RANK_WEIGHTS["base"]
        + persist * RANK_WEIGHTS["vol_persistence"]
        + acceleration * RANK_WEIGHTS["acceleration"]
        + rs * RANK_WEIGHTS["rs"]
        + risk * RANK_WEIGHTS["risk_penalty"],
        2,
    )


SETUP_RANK_WEIGHTS: dict[str, float] = {
    "base": 0.30,
    "vol_persistence": 0.20,
    "sector_lead": 0.15,
    "pivot": 0.15,
    "liquidity": 0.10,
    "rs": 0.10,
}


def setup_rank_score(metrics: dict[str, Any]) -> float:
    """Weighted rank for PRE_BREAKOUT setups (0-100); de-emphasizes breakout pct/vol."""
    base = float(metrics.get("base_score") or _NEUTRAL_SUBSCORE)
    persist_raw = int(metrics.get("persistence_score") or 0)
    persist = persist_raw / 4.0 * 100.0
    sector = float(metrics.get("sector_lead")) if _is_finite(metrics.get("sector_lead")) else 50.0
    pivot = float(metrics.get("pivot_proximity") or _NEUTRAL_SUBSCORE)
    liq = float(metrics.get("liquidity_quality") or _NEUTRAL_SUBSCORE)
    rsi_val = float(metrics.get("rsi_val") or 50.0)
    rs = _clamp((rsi_val - 30.0) / 40.0 * 100.0, 0.0, 100.0)
    penalty = float(metrics.get("participation_penalty") or 0.0)

    raw = (
        base * SETUP_RANK_WEIGHTS["base"]
        + persist * SETUP_RANK_WEIGHTS["vol_persistence"]
        + sector * SETUP_RANK_WEIGHTS["sector_lead"]
        + pivot * SETUP_RANK_WEIGHTS["pivot"]
        + liq * SETUP_RANK_WEIGHTS["liquidity"]
        + rs * SETUP_RANK_WEIGHTS["rs"]
    )
    return round(_clamp(raw - penalty, 0.0, 100.0), 2)
