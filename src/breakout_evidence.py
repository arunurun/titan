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

# Base quality weights (user spec)
_BASE_W_COMPRESSION = 0.40
_BASE_W_TIGHT = 0.30
_BASE_W_PIVOT = 0.30

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


def liquidity_gate_pass(tier: str | None, median_turnover_inr: float | None) -> bool:
    """
    Hard liquidity gate by tier.

    Small cap: median daily turnover >= 2 crore INR.
    Micro cap: median daily turnover >= 3 crore INR.
    Missing turnover skips the gate (returns True).
    """
    if not _is_finite(median_turnover_inr):
        return True
    tier_key = _normalize_tier(tier)
    floor = SMALL_CAP_MIN_MEDIAN_TURNOVER_INR
    if tier_key == TIER_MICRO_CAP:
        floor = MICRO_CAP_MIN_MEDIAN_TURNOVER_INR
    return float(median_turnover_inr) >= floor


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


def micro_cap_participation_pass(
    tier: str | None,
    *,
    vpr: float | None = None,
    cmf: float | None = None,
    delivery_pct: float | None = None,
) -> bool | None:
    """
    Check VPR/CMF/delivery against tier thresholds.

    Returns None when required inputs are missing (skip check).
    """
    rules = micro_cap_stricter_rules(tier)
    checks: list[bool] = []

    if _is_finite(vpr):
        checks.append(float(vpr) > float(rules["vpr_min"]))
    if _is_finite(cmf):
        checks.append(float(cmf) > float(rules["cmf_min"]))

    delivery_min = rules.get("delivery_pct_min")
    if delivery_min is not None:
        if not _is_finite(delivery_pct):
            return None
        checks.append(float(delivery_pct) > float(delivery_min))

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
    atr_recent = sum(atr_arr[max(0, t - 5) : t]) / max(1, min(5, t))
    atr_prior = sum(atr_arr[max(0, t - 15) : max(0, t - 5)]) / max(1, min(10, max(0, t - 5)))
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
    pivot_proximity = (
        _clamp(100.0 * (1.0 - abs(float(closes[t]) - pivot) / pivot), 0.0, 100.0)
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


def compute_evidence_metrics(
    df: dict[str, Any],
    as_of_idx: int,
    tier_name: str,
    vol_20_avg: Sequence[float],
    *,
    bhav_turnover_lacs: float | None = None,
) -> dict[str, Any]:
    """Evidence bundle for scanner integration at bar T."""
    volumes = df["volume"]
    t = as_of_idx
    inputs = compute_evidence_inputs(df, as_of_idx, bhav_turnover_lacs=bhav_turnover_lacs)

    median_inr = inputs["median_turnover_inr"]
    liq_ok = liquidity_gate_pass(tier_name, median_inr)
    liq_quality = liquidity_quality_score(
        median_inr,
        None,
        inputs["vol_consistency"],
        None,
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
        "liquidity_gate_fail": None if liq_ok else "pre_filter_liquidity",
        "liquidity_quality": liq_quality,
        "median_turnover_inr": round(float(median_inr), 2) if _is_finite(median_inr) else None,
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
