"""PRE_BREAKOUT (SETUP) evaluation — coiling setups before breakout confirmation."""

from __future__ import annotations

import math
from typing import Any

try:
    from breakout_evidence import (
        TIER_MICRO_CAP,
        TIER_SMALL_CAP,
        _normalize_tier,
        compute_evidence_inputs,
        compute_evidence_metrics,
        liquidity_gate_pass,
        setup_rank_score,
    )
except ImportError:
    from .breakout_evidence import (
        TIER_MICRO_CAP,
        TIER_SMALL_CAP,
        _normalize_tier,
        compute_evidence_inputs,
        compute_evidence_metrics,
        liquidity_gate_pass,
        setup_rank_score,
    )

SIGNAL_TIER_PRE_BREAKOUT = "PRE_BREAKOUT"
SETUP_CAP_PER_TIER = 10

SETUP_PCT_CHANGE_MIN = -2.0
SETUP_PCT_CHANGE_MAX = 2.9
SETUP_VOL_MULT_MIN = 1.5
SETUP_VOL_MULT_MAX_SMALL = 2.5
SETUP_VOL_MULT_MAX_MICRO = 2.3
SETUP_BASE_SCORE_MIN = 58.0
SETUP_CONSOLIDATION_DAYS_MIN = 18
SETUP_PIVOT_PROXIMITY_MIN = 85.0
SETUP_52W_HIGH_RATIO_MIN = 0.92
SETUP_STRETCH_ATR_MAX = 2.5
SETUP_RSI_MIN = 45.0
SETUP_RSI_MAX = 68.0
SETUP_ADX_MIN = 18.0
SETUP_ADX_MAX = 32.0
SETUP_PERSISTENCE_MIN = 1
SETUP_PRE_SIGNAL_CUM_RETURN_MAX = 35.0
SETUP_MICRO_PARTICIPATION_PENALTY = 15.0

SETUP_RISK_FLAGS = (
    "SETUP — alert only. No position until trigger. "
    "Max 0.5% probe after trigger confirms PASS. Audit GSM/ASM."
)


def _is_finite(value: float | int | None) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(v) and math.isfinite(v)


def _vol_mult_max(tier_name: str) -> float:
    tier = _normalize_tier(tier_name)
    if tier == TIER_MICRO_CAP:
        return SETUP_VOL_MULT_MAX_MICRO
    return SETUP_VOL_MULT_MAX_SMALL


def setup_trigger_hit(
    df: dict[str, Any],
    as_of_idx: int,
    trigger_price: float | None,
) -> bool:
    """EOD trigger: Close[T] or High[T] reaches setup pivot (gap-up inclusive)."""
    if trigger_price is None or not _is_finite(trigger_price):
        return True
    trigger = float(trigger_price)
    close_t = float(df["close"][as_of_idx])
    high_t = float(df["high"][as_of_idx])
    return close_t >= trigger or high_t >= trigger


def _setup_trigger_price(inputs: dict[str, Any], df: dict[str, Any], as_of_idx: int) -> float | None:
    highs = df["high"]
    t = as_of_idx
    if t < 1:
        return None
    cons_high = max(float(h) for h in highs[max(0, t - 30) : t])
    return round(cons_high, 2) if cons_high > 0 else None


def _52w_high_ratio(df: dict[str, Any], as_of_idx: int) -> float | None:
    highs = df["high"]
    closes = df["close"]
    t = as_of_idx
    n = t + 1
    lookback = min(252, n - 1)
    if lookback <= 0:
        return None
    start = max(0, n - 1 - lookback)
    high_52w = max(float(h) for h in highs[start:n])
    if high_52w <= 0:
        return None
    return float(closes[t]) / high_52w


def _pre_signal_cum_return(df: dict[str, Any], as_of_idx: int) -> float | None:
    closes = df["close"]
    t = as_of_idx
    if t < 10:
        return None
    c_t1 = float(closes[t - 1])
    c_t10 = float(closes[t - 10])
    if c_t10 <= 0:
        return None
    return (c_t1 / c_t10 - 1.0) * 100.0


def evaluate_setup_as_of(
    df: dict[str, Any],
    as_of_idx: int,
    tier_name: str,
    *,
    min_price: float,
    vol_mult_threshold: float,
    bhav_turnover_lacs: float | None = None,
    delivery_pct: float | None = None,
    free_float_pct: float | None = None,
    sector_lead: float | None = None,
    rsi_val: float | None = None,
    adx_val: float | None = None,
    pct_change: float | None = None,
    vol_mult: float | None = None,
    sma20_last: float | None = None,
    sma50_last: float | None = None,
    vol_20_avg: list[float] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate PRE_BREAKOUT setup criteria at bar T.

    Returns a metrics dict with ``signal_tier=PRE_BREAKOUT`` when all gates pass,
    otherwise ``None``.
    """
    n = as_of_idx + 1
    if n < 50:
        return None

    close = df["close"][:n]
    volume = df["volume"][:n]
    latest_price = float(close[-1])

    if latest_price < min_price:
        return None

    if pct_change is None:
        prev_price = float(close[-2])
        pct_change = ((latest_price - prev_price) / prev_price) * 100.0 if prev_price > 0 else 0.0

    if vol_20_avg is None:
        try:
            from breakout_scanner import calculate_sma as _calc_sma
        except ImportError:
            from .breakout_scanner import calculate_sma as _calc_sma
        vol_20_avg = _calc_sma(volume, 20)

    if vol_mult is None:
        vma = vol_20_avg[-1] if vol_20_avg else 1.0
        vol_mult = float(volume[-1]) / (vma if vma > 0 else 1.0)

    if rsi_val is None or adx_val is None or sma20_last is None or sma50_last is None:
        try:
            from breakout_scanner import calculate_rsi, calculate_adx, calculate_sma
        except ImportError:
            from .breakout_scanner import calculate_rsi, calculate_adx, calculate_sma
        sma_20 = calculate_sma(close, 20)
        sma_50 = calculate_sma(close, 50)
        rsi = calculate_rsi(close, 14)
        adx_arr, _, _ = calculate_adx(df["high"][:n], df["low"][:n], close, period=14)
        rsi_val = float(rsi[-1])
        adx_val = float(adx_arr[-1])
        sma20_last = float(sma_20[-1])
        sma50_last = float(sma_50[-1])

    evidence = compute_evidence_metrics(
        df,
        as_of_idx,
        tier_name,
        vol_20_avg,
        bhav_turnover_lacs=bhav_turnover_lacs,
        delivery_pct=delivery_pct,
        free_float_pct=free_float_pct,
    )
    inputs = compute_evidence_inputs(df, as_of_idx, bhav_turnover_lacs=bhav_turnover_lacs)

    median_inr = evidence.get("median_turnover_inr")
    session_notional = latest_price * float(volume[-1]) if _is_finite(volume[-1]) else None
    if not liquidity_gate_pass(
        tier_name, median_inr, session_notional_inr=session_notional,
    ):
        return None

    if evidence.get("breakout_stage") == 3:
        return None

    cum_ret = _pre_signal_cum_return(df, as_of_idx)
    if _is_finite(cum_ret) and float(cum_ret) > SETUP_PRE_SIGNAL_CUM_RETURN_MAX:
        return None

    vol_max = _vol_mult_max(tier_name)
    if not (SETUP_PCT_CHANGE_MIN <= float(pct_change) <= SETUP_PCT_CHANGE_MAX):
        return None
    if not (SETUP_VOL_MULT_MIN <= float(vol_mult) <= vol_max):
        return None

    persist = int(evidence.get("persistence_score") or 0)
    if persist < SETUP_PERSISTENCE_MIN:
        return None

    base_score = float(evidence.get("base_score") or 0.0)
    if base_score < SETUP_BASE_SCORE_MIN:
        return None

    cons_days = int(inputs.get("consolidation_days") or 0)
    if cons_days < SETUP_CONSOLIDATION_DAYS_MIN:
        return None

    pivot_prox = inputs.get("pivot_proximity")
    if not _is_finite(pivot_prox) or float(pivot_prox) < SETUP_PIVOT_PROXIMITY_MIN:
        return None

    ratio_52w = _52w_high_ratio(df, as_of_idx)
    if ratio_52w is None or ratio_52w < SETUP_52W_HIGH_RATIO_MIN:
        return None

    stretch = inputs.get("stretch_atr")
    if not _is_finite(stretch) or float(stretch) >= SETUP_STRETCH_ATR_MAX:
        return None

    trend_ok = latest_price >= sma50_last or (
        latest_price >= sma20_last and float(vol_mult) >= SETUP_VOL_MULT_MIN
    )
    if not trend_ok:
        return None

    if not (SETUP_RSI_MIN <= float(rsi_val) <= SETUP_RSI_MAX):
        return None
    if not (SETUP_ADX_MIN <= float(adx_val) <= SETUP_ADX_MAX):
        return None

    part_pass = evidence.get("micro_participation_pass")
    participation_penalty = 0.0
    if part_pass is False:
        participation_penalty = SETUP_MICRO_PARTICIPATION_PENALTY

    trigger_price = _setup_trigger_price(inputs, df, as_of_idx)
    rank_metrics = {
        "pct_change": float(pct_change),
        "vol_mult": float(vol_mult),
        "base_score": base_score,
        "persistence_score": persist,
        "rsi_val": float(rsi_val),
        "liquidity_quality": evidence.get("liquidity_quality"),
        "sector_lead": sector_lead,
        "pivot_proximity": float(pivot_prox) if _is_finite(pivot_prox) else None,
        "participation_penalty": participation_penalty,
    }
    setup_rank = setup_rank_score(rank_metrics)

    return {
        "signal_tier": SIGNAL_TIER_PRE_BREAKOUT,
        "passed": False,
        "fail_reason": None,
        "latest_price": round(latest_price, 2),
        "pct_change": round(float(pct_change), 4),
        "vol_mult": round(float(vol_mult), 4),
        "rsi_val": round(float(rsi_val), 2),
        "adx_val": round(float(adx_val), 2),
        "base_score": base_score,
        "persistence_score": persist,
        "liquidity_quality": evidence.get("liquidity_quality"),
        "breakout_stage": evidence.get("breakout_stage"),
        "pivot_proximity": round(float(pivot_prox), 2) if _is_finite(pivot_prox) else None,
        "setup_trigger_price": trigger_price,
        "setup_trigger_vol_mult": vol_mult_threshold,
        "setup_trigger_pct_min": 3.0,
        "setup_rank": setup_rank,
        "risk_flags": SETUP_RISK_FLAGS,
        "micro_participation_penalty": participation_penalty,
        "delivery_pct": evidence.get("delivery_pct"),
        "vpr": evidence.get("vpr"),
        "cmf": evidence.get("cmf"),
        "sector_lead": sector_lead,
        "consolidation_days": cons_days,
        "stretch_atr": stretch,
        "52w_high_ratio": round(ratio_52w, 4) if ratio_52w is not None else None,
    }
