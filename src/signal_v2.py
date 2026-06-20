"""V2 signal engine: a layered (A-E) waterfall over the same audit dict.

``action_signals.derive_action_signal`` always routes here. Layers A–E always run;
``accumulate`` is a first-class label. Tunable thresholds use ``TITAN_SIGV2_*`` env vars.

Layers:
  A  data-quality / sanity      -> may only withhold buy / downgrade confidence
  B  hard disqualifiers         -> two-tier (instant-exit whitelist + corroboration)
  C  graded evidence            -> ramped legacy families + money-flow + over-extension
  D  context modifiers          -> reshape C weights (ADX regime, divergence, pullback,
                                   stale-flow OBV tiebreaker); never early-returns a label
  E  mapping + hysteresis        -> risk_net -> label + confidence

All numeric values below are *tunable defaults*; the spec config keys override them via
environment variables, read with the same ``_env_truthy("TITAN_*")`` pattern used in
``analysis_store``.
"""

from __future__ import annotations

import math
import os
from typing import Any

# ---------------------------------------------------------------------------
# config helpers (mirror analysis_store._env_truthy semantics)
# ---------------------------------------------------------------------------


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _buy_risk_ceiling() -> float:
    return _env_float("TITAN_SIGV2_E_BUY_RISK_MAX", _SIGV2_BUY_RISK_MAX)


def _trim_risk_floor() -> float:
    return _env_float("TITAN_SIGV2_E_TRIM_RISK_MIN", _SIGV2_TRIM_RISK_MIN)


def _exit_risk_floor() -> float:
    return _env_float("TITAN_SIGV2_E_EXIT_RISK_MIN", _SIGV2_EXIT_RISK_MIN)


def _obv_trend_confirm(audit: dict[str, Any]) -> bool | None:
    """Read OBV trend confirm from audit; None when unavailable."""
    raw = audit.get("obv_trend_confirm")
    if raw is None:
        obv = _sf(audit.get("obv_latest"))
        ema = _sf(audit.get("obv_ema_20"))
        if math.isnan(obv) or math.isnan(ema):
            return None
        return obv > ema
    return bool(raw)


def _adx_plus_di(audit: dict[str, Any]) -> float:
    return _sf(audit.get("adx_plus_di_14", audit.get("plus_di_14")))


def _adx_minus_di(audit: dict[str, Any]) -> float:
    return _sf(audit.get("adx_minus_di_14", audit.get("minus_di_14")))


def _bullish_adx_trend(audit: dict[str, Any]) -> bool:
    adx = _sf(audit.get("adx_14"))
    plus_di = _adx_plus_di(audit)
    minus_di = _adx_minus_di(audit)
    adx_strong = _env_float("TITAN_SIGV2_D_ADX_STRONG", 25.0)
    return (
        not math.isnan(adx)
        and adx >= adx_strong
        and not math.isnan(plus_di)
        and not math.isnan(minus_di)
        and plus_di > minus_di
    )


def _bearish_adx_trend(audit: dict[str, Any]) -> bool:
    adx = _sf(audit.get("adx_14"))
    plus_di = _adx_plus_di(audit)
    minus_di = _adx_minus_di(audit)
    adx_strong = _env_float("TITAN_SIGV2_D_ADX_STRONG", 25.0)
    return (
        not math.isnan(adx)
        and adx >= adx_strong
        and not math.isnan(plus_di)
        and not math.isnan(minus_di)
        and minus_di > plus_di
    )


def _suppress_volatility_penalty(audit: dict[str, Any]) -> bool:
    """Skip ATR volatility accumulation in strong bullish ADX trends."""
    return _bullish_adx_trend(audit)


def v2_enabled() -> bool:
    """V2 is always the production signal path."""
    return True


_FAMILY_CAP_LIMITS = {
    "price": 4.0,
    "flow": 2.0,
    "extension": 2.0,
    "volatility": 2.0,
}


def _cap_family_groups(groups: dict[str, float]) -> dict[str, float]:
    return {
        key: min(_clamp(val, 0.0, 10.0), _FAMILY_CAP_LIMITS[key])
        for key, val in groups.items()
    }


def _apply_family_caps_to_risk(
    *,
    fam: dict[str, float],
    momentum_scaled: float,
    money_flow_bear: float,
    over_extension: float,
    upside_z: float,
    volatility: float,
    fundamental: float,
) -> tuple[float, dict[str, float]]:
    """Group Layer-C bear terms and cap each family before summing."""
    price = momentum_scaled + _sf(fam.get("trend", 0.0)) + _sf(fam.get("z", 0.0))
    groups = {
        "price": price,
        "flow": money_flow_bear,
        "extension": over_extension + upside_z,
        "volatility": volatility,
    }
    capped = _cap_family_groups(groups)
    risk_c = (
        _sf(fam.get("horizon", 0.0))
        + _sf(fam.get("intent", 0.0))
        + sum(capped.values())
        + fundamental
    )
    return risk_c, capped


def _ipo_leader_precheck(audit: dict[str, Any]) -> bool:
    intent_min = _env_float("TITAN_IPO_LEADER_INTENT_MIN", 75.0)
    nw_min = _env_float("TITAN_IPO_LEADER_NW_MIN", 70.0)
    vpr_min = _env_float("TITAN_IPO_LEADER_VPR_MIN", 2.0)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    vpr = _participation_vpr(audit)
    cmf = _sf(audit.get("cmf_20"))
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(nw)
        and nw >= nw_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
        and not math.isnan(cmf)
        and cmf > 0.05
    )


def _ipo_leader_buy_ok(audit: dict[str, Any], risk_net: float) -> bool:
    risk_max = _env_float("TITAN_IPO_LEADER_RISK_MAX", 2.0)
    return risk_net < risk_max and _ipo_leader_precheck(audit)


_MOMENTUM_SECTORS = frozenset({
    "renewables_clean_energy",
    "railways_transport_infra",
    "power_utilities",
    "defence",
})


def _tier2_thresholds(audit: dict[str, Any]) -> tuple[int, int]:
    trim = _env_int("TITAN_SIGV2_B_TIER2_TRIM_COUNT", 2)
    exit_c = _env_int("TITAN_SIGV2_B_TIER2_EXIT_COUNT", 3)
    sector_clean = str(audit.get("sector_key") or audit.get("sector") or "").strip().lower()
    is_momentum_sector = any(
        m_sec.lower().strip() in sector_clean for m_sec in _MOMENTUM_SECTORS
    )
    if is_momentum_sector:
        trim = 3
        exit_c = 4
    return trim, exit_c


def _overext_counts_as_corroborator(audit: dict[str, Any], c: dict[str, Any]) -> bool:
    if not bool(c.get("over_extension_hot")):
        return False
    cmf = _sf(audit.get("cmf_20"))
    distribution = (not math.isnan(cmf)) and cmf < -0.05
    rel5 = _sf(audit.get("rel_return_5d_vs_nifty_pct"))
    weak_rel = (not math.isnan(rel5) and rel5 <= -3.0)
    return distribution or weak_rel


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

CORE_METRICS: tuple[str, ...] = (
    "z_score",
    "cmf_20",
    "adx_14",
    "ema_200_distance_pct",
    "atr_14_pct",
    "return_1d_pct",
    "return_5d_pct",
    "return_21d_pct",
    "return_63d_pct",
)

_SEVERITY: dict[str, int] = {
    "buy": 0,
    "accumulate": 1,
    "hold": 2,
    "trim": 3,
    "exit-risk": 4,
}

_SIGV2_BUY_RISK_MAX = 3.0
_SIGV2_TRIM_RISK_MIN = 5.0
_SIGV2_EXIT_RISK_MIN = 7.0


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _spot_vs_strike_pct(spot: float, strike: float) -> float:
    if math.isnan(spot) or math.isnan(strike) or strike == 0.0:
        return float("nan")
    return ((spot / strike) - 1.0) * 100.0


def _options_into_call_wall(audit: dict[str, Any]) -> bool:
    """Tier-2 corroborator: spot within ~1% of call OI wall with bearish/trim context."""
    if bool(audit.get("option_chain_unavailable", True)):
        return False
    spot = _sf(audit.get("close_last"))
    call_wall = _sf(audit.get("call_oi_wall_strike"))
    if math.isnan(spot) or math.isnan(call_wall):
        return False
    near_wall = abs(_spot_vs_strike_pct(spot, call_wall)) <= 1.0
    if not near_wall:
        return False
    sell = str(audit.get("sell_signal") or "").lower()
    cmf = _sf(audit.get("cmf_20"))
    ret1d = _sf(audit.get("return_1d_pct"))
    z = _sf(audit.get("z_score"))
    bearish_context = (
        sell in ("trim", "exit-risk")
        or (not math.isnan(cmf) and cmf < -0.05)
        or (not math.isnan(ret1d) and ret1d < 0.0)
        or (not math.isnan(z) and z < 0.0)
    )
    return bearish_context


def _options_below_put_support(audit: dict[str, Any]) -> bool:
    """Tier-2 corroborator: spot below put OI wall with distribution/negative tape."""
    if bool(audit.get("option_chain_unavailable", True)):
        return False
    spot = _sf(audit.get("close_last"))
    put_wall = _sf(audit.get("put_oi_wall_strike"))
    if math.isnan(spot) or math.isnan(put_wall) or spot >= put_wall:
        return False
    cmf = _sf(audit.get("cmf_20"))
    ret1d = _sf(audit.get("return_1d_pct"))
    return (
        (not math.isnan(cmf) and cmf < -0.05)
        or (not math.isnan(ret1d) and ret1d < 0.0)
        or bool(audit.get("high_volume_down_day_proxy"))
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ramp(value: float, zero_at: float, full_at: float, full_points: float) -> float:
    """Linear ramp: 0 points at ``zero_at`` -> ``full_points`` at ``full_at`` (clamped).

    Direction-agnostic: works whether ``full_at`` is above or below ``zero_at`` because
    the fraction is normalized and clamped to [0, 1]. NaN input -> 0.
    """
    if math.isnan(value) or zero_at == full_at:
        return 0.0
    frac = (value - zero_at) / (full_at - zero_at)
    return _clamp(frac, 0.0, 1.0) * full_points


def _round(x: float, n: int = 4) -> float:
    return round(x, n) if not math.isnan(x) else float("nan")


# ---------------------------------------------------------------------------
# Layer A: data-quality / sanity (withhold / downgrade only)
# ---------------------------------------------------------------------------


def layer_a(audit: dict[str, Any]) -> dict[str, Any]:
    """Returns buy_allowed, label_ceiling, confidence_seed, reasons.

    Can ONLY downgrade confidence or withhold buy/accumulate; never asserts buy.
    """
    out: dict[str, Any] = {
        "buy_allowed": True,
        "label_ceiling": None,
        "confidence_seed": 1.0,
        "reasons": [],
    }

    nan_max = _env_int("TITAN_SIGV2_A_NAN_MAX", 3)
    short_hist_conf = _env_float("TITAN_SIGV2_A_SHORT_HISTORY_CONF", 0.6)

    reasons: list[str] = []
    seed = 1.0

    nan_count = sum(1 for k in CORE_METRICS if math.isnan(_sf(audit.get(k))))
    # Per-metric NaN is cautionary, never free: each shaves a little confidence.
    if nan_count > 0:
        seed *= max(0.0, 1.0 - 0.05 * nan_count)
        reasons.append(f"{nan_count} core metric(s) unavailable")
    if nan_count >= nan_max:
        out["buy_allowed"] = False
        seed *= 0.5
        reasons.append(f"NaN census >= {nan_max}: buy withheld")

    if bool(audit.get("history_lt_200_sessions")):
        if _ipo_leader_precheck(audit):
            out["label_ceiling"] = None
            reasons.append("short history: IPO leader precheck passed (buy allowed if risk ok)")
        else:
            out["buy_allowed"] = False
            out["label_ceiling"] = "accumulate"
            seed *= short_hist_conf
            reasons.append("short history (<200 sessions): ceiling=accumulate")

    if bool(audit.get("liquidity_thin_proxy")):
        out["buy_allowed"] = False
        reasons.append("thin liquidity: buy forbidden")

    out["confidence_seed"] = _clamp(seed, 0.0, 1.0)
    out["reasons"] = reasons
    return out


# ---------------------------------------------------------------------------
# Layer C: graded evidence (ramps + money-flow + over-extension)
# ---------------------------------------------------------------------------


def _family_points(audit: dict[str, Any]) -> dict[str, Any]:
    """Legacy risk families re-expressed as linear ramps; per-family caps preserved."""
    trace: list[dict[str, Any]] = []
    fam: dict[str, float] = {}

    next_week = _sf(audit.get("next_week_score"))
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _sf(audit.get("z_score"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ret21d = _sf(audit.get("return_21d_pct"))
    ret63d = _sf(audit.get("return_63d_pct"))
    ret126d = _sf(audit.get("return_126d_pct"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    atr_pct = _sf(audit.get("atr_14_pct"))
    atr_pi = _sf(audit.get("atr_penalty_input"))

    def add(group: str, metric: str, value: float, points: float) -> None:
        if points > 0.05:
            trace.append(
                {
                    "layer": "C",
                    "group": group,
                    "metric": metric,
                    "value": _round(value),
                    "points": _round(points),
                    "side": "bear",
                }
            )

    # Horizon (cap 3): ramp 55 -> 45
    h = _ramp(next_week, zero_at=55.0, full_at=45.0, full_points=3.0)
    fam["horizon"] = min(3.0, h)
    add("horizon", "next_week_score", next_week, fam["horizon"])

    # Intent (cap 2): ramp 52 -> 45
    inc = _ramp(eff, zero_at=52.0, full_at=45.0, full_points=2.0)
    fam["intent"] = min(2.0, inc)
    add("intent", "effective_intent_score", eff, fam["intent"])

    # Downside Z only (cap 2): ramp -1 -> -2
    zc = _ramp(z, zero_at=-1.0, full_at=-2.0, full_points=2.0)
    fam["z"] = min(2.0, zc)
    add("z", "z_score", z, fam["z"])

    # Medium-term composite: 1d excluded (reserved for extreme_price_move_proxy / Layer A).
    unit = 3.0
    r5 = _ramp(ret5d, zero_at=-2.0, full_at=-6.0, full_points=unit)
    r21 = _ramp(ret21d, zero_at=-4.0, full_at=-12.0, full_points=unit)
    r63 = _ramp(ret63d, zero_at=-8.0, full_at=-20.0, full_points=unit)
    r126 = _ramp(ret126d, zero_at=-12.0, full_at=-30.0, full_points=unit)
    fam["momentum"] = min(
        unit, 0.10 * r5 + 0.25 * r21 + 0.35 * r63 + 0.30 * r126
    )
    add("momentum", "return_5d/21d/63d/126d_pct", ret5d, fam["momentum"])

    # Below-EMA200 trend only (cap 2): ramp -2 -> -6 (over-extension is C-8)
    em = _ramp(ema_dist, zero_at=-2.0, full_at=-6.0, full_points=2.0)
    fam["trend"] = min(2.0, em)
    add("trend", "ema_200_distance_pct", ema_dist, fam["trend"])

    # Volatility (cap 2): prefer ATR-vs-sector input, else raw ATR%
    # Suppressed in strong bullish ADX (+DI>-DI) to avoid double-count with stretch.
    if not _suppress_volatility_penalty(audit):
        if not math.isnan(atr_pi):
            vo = _ramp(atr_pi, zero_at=1.25, full_at=2.2, full_points=2.0)
            vmetric, vval = "atr_penalty_input", atr_pi
        else:
            vo = _ramp(atr_pct, zero_at=4.0, full_at=6.0, full_points=2.0)
            vmetric, vval = "atr_14_pct", atr_pct
        fam["volatility"] = min(2.0, vo)
        add("volatility", vmetric, vval, fam["volatility"])
    else:
        fam["volatility"] = 0.0

    return {"families": fam, "trace": trace}


def layer_c(audit: dict[str, Any]) -> dict[str, Any]:
    """Graded evidence. Returns family points, money-flow + over-extension terms,
    bull contributions, and a structured reason_trace.

    Layer D multipliers are NOT applied here; they are applied at aggregation so the
    raw per-term values stay inspectable in the trace.
    """
    fp = _family_points(audit)
    trace: list[dict[str, Any]] = list(fp["trace"])

    cmf = _sf(audit.get("cmf_20"))
    obv_confirm = _obv_trend_confirm(audit)
    try:
        from stretch_engine import effective_stretch_atr

        stretch = effective_stretch_atr(audit)
    except ImportError:
        stretch = _sf(audit.get("ema200_stretch_atr"))
    stretch_pctile = _sf(audit.get("sector_pctile_ema200_stretch"))
    z = _sf(audit.get("z_score"))

    k_cmf = _env_float("TITAN_SIGV2_C_CMF_K", 10.0)
    cap_cmf = _env_float("TITAN_SIGV2_C_CMF_CAP", 2.0)
    stretch_deadband = _env_float("TITAN_SIGV2_C_STRETCH_DEADBAND_ATR", 3.0)
    if _bullish_adx_trend(audit):
        stretch_deadband *= _env_float("TITAN_SIGV2_C_STRETCH_DEADBAND_BULL_MULT", 1.5)
    if bool(audit.get("regime_ignore_mild_overextension")):
        stretch_deadband *= _env_float("TITAN_SIGV2_C_STRETCH_DEADBAND_BULL_MULT", 1.5)
    stretch_ramp = _env_float("TITAN_SIGV2_C_STRETCH_RAMP_ATR", 8.0)
    stretch_cap = _env_float("TITAN_SIGV2_C_STRETCH_CAP", 2.0)
    # Fix A (mirror): symmetric upside-z over-extension. A statistically stretched
    # *up* move (high positive z) adds mean-reversion risk, paralleling the existing
    # downside-z bear term. Conservative default deadband keeps clean buys intact.
    upside_z_deadband = _env_float("TITAN_SIGV2_C_UPSIDE_Z_DEADBAND", 2.5)
    upside_z_ramp = _env_float("TITAN_SIGV2_C_UPSIDE_Z_RAMP", 4.0)
    upside_z_cap = _env_float("TITAN_SIGV2_C_UPSIDE_Z_CAP", 1.5)

    # C-7 money flow with +/-0.05 dead-band, scaled by magnitude.
    money_flow_bear = 0.0
    money_flow_bull = 0.0
    if not math.isnan(cmf):
        if cmf < -0.05:
            money_flow_bear = _clamp((-0.05 - cmf) * k_cmf, 0.0, cap_cmf)
            if money_flow_bear > 0.0 and obv_confirm is False:
                money_flow_bear *= 1.25
            if money_flow_bear > 0.05:
                trace.append(
                    {
                        "layer": "C", "group": "money_flow", "metric": "cmf_20",
                        "value": _round(cmf), "points": _round(money_flow_bear), "side": "bear",
                    }
                )
        elif cmf > 0.05:
            money_flow_bull = _clamp((cmf - 0.05) * k_cmf, 0.0, cap_cmf)
            if money_flow_bull > 0.0 and obv_confirm is True:
                money_flow_bull *= 1.25
            if money_flow_bull > 0.05:
                trace.append(
                    {
                        "layer": "C", "group": "money_flow", "metric": "cmf_20",
                        "value": _round(cmf), "points": _round(money_flow_bull), "side": "bull",
                    }
                )

    # C-8 ATR-normalized over-extension (upside only; negatives ramp to 0).
    over_ext = _ramp(stretch, zero_at=stretch_deadband, full_at=stretch_ramp, full_points=stretch_cap)
    over_extension_hot = (not math.isnan(stretch)) and stretch >= stretch_deadband
    # Secondary corroborating modifier: top-decile sector stretch amplifies x1.25.
    if over_ext > 0.0 and not math.isnan(stretch_pctile) and stretch_pctile >= 90.0:
        over_ext *= 1.25
    if over_ext > 0.05:
        trace.append(
            {
                "layer": "C", "group": "over_extension", "metric": "ema200_stretch_atr",
                "value": _round(stretch), "points": _round(over_ext), "side": "bear",
            }
        )

    # C-8b upside-z over-extension (symmetric to the downside-z bear term).
    upside_z = _ramp(z, zero_at=upside_z_deadband, full_at=upside_z_ramp, full_points=upside_z_cap)
    if upside_z > 0.05:
        trace.append(
            {
                "layer": "C", "group": "over_extension", "metric": "z_score",
                "value": _round(z), "points": _round(upside_z), "side": "bear",
            }
        )

    # Fundamentals (kept from legacy as a graded adjustment to bear risk).
    f_status = str(audit.get("fundamental_status") or "unavailable")
    fundamental = 0.0
    if f_status == "weak":
        fundamental = 2.0
    elif f_status == "balanced":
        fundamental = 1.0
    elif f_status == "strong":
        fundamental = -1.0
    if abs(fundamental) > 0.05:
        trace.append(
            {
                "layer": "C", "group": "fundamental", "metric": "fundamental_status",
                "value": f_status, "points": _round(fundamental),
                "side": "bear" if fundamental > 0 else "bull",
            }
        )

    return {
        "families": fp["families"],
        "money_flow_bear": money_flow_bear,
        "money_flow_bull": money_flow_bull,
        "over_extension": over_ext,
        "over_extension_hot": over_extension_hot,
        "upside_z": upside_z,
        "fundamental": fundamental,
        "bull_terms": money_flow_bull,
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# Layer D: context modifiers (reshape weights; never early-return a label)
# ---------------------------------------------------------------------------


def layer_d(audit: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """Returns weight multipliers, additive bumps, flags. Does not produce a label."""
    out: dict[str, Any] = {
        "mult_money_flow": 1.0,
        "mult_over_extension": 1.0,
        "mult_momentum": 1.0,
        "mult_risk": 1.0,
        "layer_c_risk_mult": 1.0,
        "divergence_bump": 0.0,
        "buy_confidence_cap": None,
        "pullback_bull_bump": 0.0,
        "staleflow_downgrade": False,
        "reasons": [],
    }

    reasons: list[str] = []
    adx = _sf(audit.get("adx_14"))
    cmf = _sf(audit.get("cmf_20"))
    obv_confirm = _obv_trend_confirm(audit)
    ret1d = _sf(audit.get("return_1d_pct"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    vpr = _sf(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))

    adx_weak = _env_float("TITAN_SIGV2_D_ADX_WEAK", 20.0)
    adx_strong = _env_float("TITAN_SIGV2_D_ADX_STRONG", 25.0)
    divergence_ret1d = _env_float("TITAN_SIGV2_D_DIVERGENCE_RET1D", 2.0)
    pullback_vpr = _env_float("TITAN_SIGV2_D_PULLBACK_VPR", 1.0)

    prev_mults = audit.get("prev_adx_regime_mults")
    if not isinstance(prev_mults, dict):
        prev_mults = {}

    # 1) Directional ADX regime switching with 20-25 deadband persistence.
    if not math.isnan(adx):
        if adx < adx_weak:
            out["mult_money_flow"] = 1.3
            out["mult_over_extension"] = 1.3
            out["mult_momentum"] = 0.7
            out["mult_risk"] = 1.0
            reasons.append(f"weak ADX {adx:.1f}: mean-reversion up-weighted")
        elif adx >= adx_strong:
            plus_di = _adx_plus_di(audit)
            minus_di = _adx_minus_di(audit)
            if not math.isnan(plus_di) and not math.isnan(minus_di) and plus_di > minus_di:
                out["mult_momentum"] = 1.3
                out["mult_risk"] = 0.8
                reasons.append(
                    f"strong ADX {adx:.1f} (+DI={plus_di:.1f}, -DI={minus_di:.1f}): momentum up, vol noise down"
                )
            elif not math.isnan(plus_di) and not math.isnan(minus_di) and minus_di > plus_di:
                out["mult_momentum"] = 0.5
                out["mult_risk"] = 1.5
                reasons.append(
                    f"strong ADX {adx:.1f} (+DI={plus_di:.1f}, -DI={minus_di:.1f}): momentum down, risk up"
                )
            else:
                out["mult_momentum"] = 1.3
                out["mult_risk"] = 0.8
                reasons.append(
                    f"strong ADX {adx:.1f} (+DI={plus_di:.1f}, -DI={minus_di:.1f}): momentum up-weighted (DI tie)"
                )
        else:
            for key, attr in (
                ("mult_money_flow", "mult_money_flow"),
                ("mult_over_extension", "mult_over_extension"),
                ("mult_momentum", "mult_momentum"),
                ("mult_risk", "mult_risk"),
            ):
                val = _sf(prev_mults.get(key, 1.0))
                out[attr] = 1.0 if math.isnan(val) else val
            reasons.append(f"ADX deadband {adx:.1f}: persisting prior regime multipliers")

    out["adx_regime_mults"] = {
        "mult_money_flow": out["mult_money_flow"],
        "mult_over_extension": out["mult_over_extension"],
        "mult_momentum": out["mult_momentum"],
        "mult_risk": out["mult_risk"],
    }

    # 2) Money-flow divergence ("hollow breakout"): price up on net distribution
    #    without OBV trend confirmation (distribution not absorbed on tape).
    if (
        (not math.isnan(ret1d) and ret1d > divergence_ret1d)
        and (not math.isnan(cmf) and cmf < -0.05)
        and obv_confirm is False
    ):
        out["divergence_bump"] = 1.0
        out["buy_confidence_cap"] = 0.5
        reasons.append(f"hollow-breakout divergence (ret1d {ret1d:.2f}%, cmf {cmf:.3f})")

    # 2b) Institutional absorption: down day with positive CMF and OBV above EMA.
    # Halves Layer-C bear risk in _aggregate via layer_c_risk_mult (see _aggregate).
    if (
        (not math.isnan(ret1d) and ret1d < 0.0)
        and (not math.isnan(cmf) and cmf > 0.05)
        and obv_confirm is True
    ):
        out["pullback_bull_bump"] = max(float(out["pullback_bull_bump"]), 0.75)
        out["layer_c_risk_mult"] = 0.5
        reasons.append("institutional absorption: CMF+ / OBV confirm on down day")

    # 3) Healthy-pullback rescue: low-volume down day with intact flow & trend.
    if (
        (not math.isnan(ret1d) and ret1d < 0.0)
        and (not math.isnan(vpr) and vpr < pullback_vpr)
        and (not math.isnan(cmf) and cmf > 0.05)
        and (not math.isnan(ret5d) and ret5d >= -3.0)
        and (not math.isnan(ema_dist) and ema_dist >= 0.0)
    ):
        out["mult_momentum"] *= 0.5
        out["pullback_bull_bump"] = max(float(out["pullback_bull_bump"]), 0.5)
        reasons.append("healthy low-volume pullback: momentum penalty halved")

    # 4) Stale-flow OBV tiebreaker: neutral flow + over-extended + weak ADX + OBV below EMA.
    if (
        (not math.isnan(cmf) and -0.05 <= cmf <= 0.05)
        and bool(c.get("over_extension_hot"))
        and (not math.isnan(adx) and adx < adx_weak)
        and obv_confirm is not True
    ):
        out["staleflow_downgrade"] = True
        reasons.append("stale-flow: neutral CMF + over-extended + weak ADX + OBV below EMA")

    out["reasons"] = reasons
    return out


# ---------------------------------------------------------------------------
# Layer B: two-tier hard disqualifiers
# ---------------------------------------------------------------------------


def _liquidity_floor_inr() -> float:
    raw = (os.environ.get("TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR") or "").strip()
    if not raw:
        return 1_200_000.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1_200_000.0


def layer_b(audit: dict[str, Any], c: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    """Two-tier overrides. Returns forced_label, bypass_hysteresis, corroborator count."""
    out: dict[str, Any] = {
        "forced_label": None,
        "bypass_hysteresis": False,
        "corroborators": 0,
        "reasons": [],
    }

    reasons: list[str] = []
    tier1_gap_pct = _env_float("TITAN_SIGV2_B_TIER1_GAP_PCT", -8.0)
    trim_count, exit_count = _tier2_thresholds(audit)

    ret1d = _sf(audit.get("return_1d_pct"))
    med_notional = _sf(audit.get("median_notional_inr_20d"))

    # ---- Tier 1: instant-exit whitelist (single signal; bypasses hysteresis) ----
    structural = bool(audit.get("structural_break_proxy"))
    gap_down = bool(audit.get("gap_down_proxy"))
    t1_structural = structural and (
        (not math.isnan(ret1d) and ret1d <= tier1_gap_pct) or gap_down
    )
    t1_liquidity = bool(audit.get("liquidity_thin_proxy")) and (
        not math.isnan(med_notional) and 0.0 < med_notional < _liquidity_floor_inr()
    )
    if t1_structural or t1_liquidity:
        out["forced_label"] = "exit-risk"
        out["bypass_hysteresis"] = True
        if t1_structural:
            reasons.append("Tier-1: structural break + gap/severe down day")
        if t1_liquidity:
            reasons.append("Tier-1: hard liquidity collapse")
        out["reasons"] = reasons
        return out

    # ---- Tier 2: corroboration-gated (>=2 distinct bearish signals) ----
    cmf = _sf(audit.get("cmf_20"))
    adx = _sf(audit.get("adx_14"))
    plus_di = _sf(audit.get("adx_plus_di_14"))
    minus_di = _sf(audit.get("adx_minus_di_14"))

    signals: list[str] = []
    # VPR-derived proxies count as ONE (de-dup): they share volume_participation_ratio.
    if (
        bool(audit.get("trap_exit_proxy"))
        or bool(audit.get("high_volume_down_day_proxy"))
        or bool(audit.get("panic_absorption_proxy"))
    ):
        signals.append("vpr-proxy stress")
    if not math.isnan(cmf) and cmf < -0.05:
        signals.append("cmf distribution")
    if _overext_counts_as_corroborator(audit, c):
        signals.append("over-extension hot")
    if (not math.isnan(adx) and adx < 20.0) and (
        not math.isnan(plus_di) and not math.isnan(minus_di) and minus_di > plus_di
    ):
        signals.append("weak ADX with -DI dominance")
    if bool(audit.get("event_risk_soon")) or bool(audit.get("event_guardrail_applied")):
        signals.append("event risk")
    if bool(d.get("staleflow_downgrade")):
        signals.append("stale-flow downgrade")
    if _options_into_call_wall(audit):
        signals.append("into call OI wall")
    if _options_below_put_support(audit):
        signals.append("below put OI support")

    count = len(signals)
    tier2_mult = float(audit.get("regime_tier2_penalty_mult", 1.0))
    effective_count = count * tier2_mult
    out["corroborators"] = count
    if effective_count >= exit_count:
        out["forced_label"] = "exit-risk"
        reasons.append(f"Tier-2: {count} corroborators -> exit-risk ({', '.join(signals)})")
    elif effective_count >= trim_count or bool(d.get("staleflow_downgrade")):
        out["forced_label"] = "trim"
        reasons.append(f"Tier-2: {count} corroborators -> trim ({', '.join(signals)})")
    out["reasons"] = reasons
    return out


# ---------------------------------------------------------------------------
# Layer E: aggregation, mapping, confidence, hysteresis
# ---------------------------------------------------------------------------


def _aggregate(c: dict[str, Any], d: dict[str, Any], audit: dict[str, Any] | None = None) -> dict[str, float]:
    """Apply Layer-D multipliers + bumps to Layer-C terms -> risk_c, bull_c."""
    if audit is None:
        audit = {}
    fam = c.get("families", {}) if isinstance(c.get("families"), dict) else {}
    mult_mom = _sf(d.get("mult_momentum", 1.0))
    mult_mf = _sf(d.get("mult_money_flow", 1.0))
    mult_oe = _sf(d.get("mult_over_extension", 1.0))
    mult_risk = _sf(d.get("mult_risk", 1.0))
    layer_c_mult = _sf(d.get("layer_c_risk_mult", 1.0))
    if math.isnan(mult_mom):
        mult_mom = 1.0
    if math.isnan(mult_mf):
        mult_mf = 1.0
    if math.isnan(mult_oe):
        mult_oe = 1.0
    if math.isnan(mult_risk):
        mult_risk = 1.0
    if math.isnan(layer_c_mult):
        layer_c_mult = 1.0

    momentum_scaled = min(3.0, _sf(fam.get("momentum", 0.0)) * mult_mom)
    money_flow_bear = _sf(c.get("money_flow_bear", 0.0)) * mult_mf
    over_extension = _sf(c.get("over_extension", 0.0)) * mult_oe
    upside_z = _sf(c.get("upside_z", 0.0)) * mult_oe
    volatility = _sf(fam.get("volatility", 0.0)) * mult_risk
    fundamental = _sf(c.get("fundamental", 0.0))

    risk_c, capped_groups = _apply_family_caps_to_risk(
        fam=fam,
        momentum_scaled=momentum_scaled,
        money_flow_bear=money_flow_bear,
        over_extension=over_extension,
        upside_z=upside_z,
        volatility=volatility,
        fundamental=0.0,
    )
    risk_c += fundamental
    audit["family_caps"] = {"groups": capped_groups}

    risk_c *= layer_c_mult
    risk_c += _sf(d.get("divergence_bump", 0.0))
    defensive_mult = _sf(audit.get("regime_defensive_penalty_mult", 1.0))
    if not math.isnan(defensive_mult) and defensive_mult > 1.0:
        risk_c *= defensive_mult
    risk_c = _clamp(risk_c, 0.0, 10.0)

    bull_c = _sf(c.get("money_flow_bull", 0.0)) * mult_mf
    bull_c += _sf(d.get("pullback_bull_bump", 0.0))
    if math.isnan(bull_c):
        bull_c = 0.0
    bull_c = _clamp(bull_c, 0.0, 10.0)
    return {"risk_c": risk_c, "bull_c": bull_c}


def _participation_vpr(audit: dict[str, Any]) -> float:
    """Best available volume-participation proxy (EOD raw, then scoring input)."""
    vpr = _sf(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))
    if not math.isnan(vpr):
        return vpr
    return _sf(
        audit.get("volume_participation_for_scoring", audit.get("absorption_for_scoring"))
    )


def _constructive_allowed(a: dict[str, Any], audit: dict[str, Any]) -> bool:
    """Layer-A buy_allowed, or thin-liquidity exception for accumulate-only paths."""
    if bool(a.get("buy_allowed")):
        return True
    if not bool(audit.get("liquidity_thin_proxy")):
        return False
    intent_min = _env_float("TITAN_SIGV2_THIN_LIQ_INTENT_MIN", _THIN_LIQ_INTENT_MIN)
    nw_min = _env_float("TITAN_SIGV2_THIN_LIQ_NW_MIN", _THIN_LIQ_NW_MIN)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(nw)
        and nw >= nw_min
    )


def _buy_gate(audit: dict[str, Any], a: dict[str, Any], c: dict[str, Any]) -> dict[str, bool]:
    """Ported legacy BUY gate + money-flow / over-extension constructive checks.

    Returns clean_buy (all constructive incl. flow & not over-extended),
    constructive_core (everything except the flow / over-extension checks),
    and constructive_scores (intent/next_week tier only).
    """
    next_week = _sf(audit.get("next_week_score"))
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    ret5d = _sf(audit.get("return_5d_pct"))
    rel5 = _sf(audit.get("rel_return_5d_vs_nifty_pct"))
    cmf = _sf(audit.get("cmf_20"))
    extreme_move = bool(audit.get("extreme_price_move_proxy"))

    buy_nw_min = _env_float("TITAN_SIGV2_BUY_NEXT_WEEK_MIN", 65.0)
    buy_intent_min = _env_float("TITAN_SIGV2_BUY_INTENT_MIN", 60.0)
    buy_delta = _sf(audit.get("regime_buy_threshold_delta"))
    if not math.isnan(buy_delta) and buy_delta != 0.0:
        buy_nw_min += buy_delta
        buy_intent_min += buy_delta

    constructive_scores = (
        not math.isnan(next_week)
        and next_week >= buy_nw_min
        and not math.isnan(eff)
        and eff >= buy_intent_min
    )
    core = (
        bool(a.get("buy_allowed"))
        and not bool(audit.get("trap_exit_proxy"))
        and not bool(audit.get("liquidity_thin_proxy"))
        and not extreme_move
        and constructive_scores
        and not (not math.isnan(ret5d) and ret5d <= -4.0)
        and not (not math.isnan(rel5) and rel5 <= -3.0)
    )
    flow_ok = math.isnan(cmf) or cmf >= -0.05
    not_overextended = not bool(c.get("over_extension_hot"))
    clean = core and flow_ok and not_overextended
    return {
        "clean_buy": clean,
        "constructive_core": core,
        "constructive_scores": constructive_scores,
    }


def _accumulate_band(audit: dict[str, Any], risk_net: float, *, buy_allowed: bool) -> bool:
    """Lower-bar accumulate when scores are decent and risk is low."""
    if not buy_allowed or risk_net >= _buy_risk_ceiling():
        return False
    nw_min = _env_float("TITAN_SIGV2_ACCUM_NEXT_WEEK_MIN", _ACCUM_NEXT_WEEK_MIN)
    intent_min = _env_float("TITAN_SIGV2_ACCUM_INTENT_MIN", _ACCUM_INTENT_MIN)
    next_week = _sf(audit.get("next_week_score"))
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    return (
        not math.isnan(next_week)
        and next_week >= nw_min
        and not math.isnan(eff)
        and eff >= intent_min
    )


def _leader_participation_floor(audit: dict[str, Any], risk_net: float, *, buy_allowed: bool) -> bool:
    """Leader + participation: strong intent with elevated VPR → at least accumulate."""
    if not buy_allowed or risk_net >= _buy_risk_ceiling():
        return False
    intent_min = _env_float("TITAN_SIGV2_LEADER_INTENT_MIN", 65.0)
    vpr_min = _env_float("TITAN_SIGV2_LEADER_VPR_MIN", 1.5)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    vpr = _participation_vpr(audit)
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
    )


def _short_history_accumulate_ok(audit: dict[str, Any], risk_net: float) -> bool:
    """Short-history names may reach accumulate (never buy) when tape is strong."""
    if risk_net >= _buy_risk_ceiling():
        return False
    intent_min = _env_float("TITAN_SIGV2_SHORT_HIST_INTENT_MIN", 65.0)
    vpr_min = _env_float("TITAN_SIGV2_SHORT_HIST_VPR_MIN", 1.5)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    vpr = _participation_vpr(audit)
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
    )


def _prior_label_from_audit(audit: dict[str, Any]) -> str | None:
    """Read normalized prior label from audit prev_action_signal."""
    prior = audit.get("prev_action_signal")
    if prior is None:
        return None
    p = str(prior).strip().lower()
    return p if p else None


def _recovery_allowed_from_defensive(
    audit: dict[str, Any] | None, risk_net: float
) -> bool:
    if audit is None:
        return False
    if audit.get("tier2_recovery_deescalation"):
        return True
    return _recovery_tape_ok(audit, risk_net, rally=True) or _recovery_tape_ok(
        audit, risk_net, rally=False
    )


def _apply_prior_defensive_deadband(
    label: str,
    risk_net: float,
    prior_label: str | None,
    *,
    audit: dict[str, Any] | None = None,
) -> str:
    """Asymmetric entry deadband after trim/exit-risk: block constructive re-entry."""
    if prior_label not in ("trim", "exit-risk"):
        return label
    buy_max = _buy_risk_ceiling()
    trim_min = _trim_risk_floor()
    recovery_ok = _recovery_allowed_from_defensive(audit, risk_net)
    if label == "buy" and risk_net >= buy_max:
        return "hold"
    if label == "accumulate" and risk_net >= buy_max:
        if buy_max <= risk_net < trim_min and recovery_ok:
            return "accumulate"
        return "hold"
    if buy_max <= risk_net < trim_min and not recovery_ok:
        return "hold"
    return label


def _map_label(
    risk_net: float,
    gate: dict[str, bool],
    audit: dict[str, Any],
    a: dict[str, Any],
    *,
    prior_label: str | None = None,
) -> str:
    """Score -> label (before forced overrides / hysteresis)."""
    if prior_label is None:
        prior_label = _prior_label_from_audit(audit)
    buy_allowed = bool(a.get("buy_allowed"))
    constructive_ok = _constructive_allowed(a, audit)
    buy_max = _buy_risk_ceiling()
    trim_min = _trim_risk_floor()
    if risk_net >= _exit_risk_floor():
        return "exit-risk"
    if risk_net >= trim_min:
        return "trim"
    # risk_net below trim floor
    if gate["clean_buy"] and risk_net < buy_max:
        label = "buy"
    elif gate["constructive_core"] and risk_net < buy_max:
        label = "accumulate"
    else:
        accum_pref_max = _env_float("TITAN_SIGV2_ACCUM_PREF_RISK_MAX", buy_max)
        if (
            constructive_ok
            and gate.get("constructive_scores")
            and risk_net < accum_pref_max
        ):
            label = "accumulate"
        elif _accumulate_band(audit, risk_net, buy_allowed=constructive_ok):
            label = "accumulate"
        elif _leader_participation_floor(audit, risk_net, buy_allowed=constructive_ok):
            label = "accumulate"
        elif _participation_accumulate_ok(audit, risk_net, buy_allowed=constructive_ok):
            label = "accumulate"
        elif (
            bool(audit.get("history_lt_200_sessions"))
            and _short_history_accumulate_ok(audit, risk_net)
        ):
            label = "accumulate"
        else:
            label = "hold"
    return _apply_prior_defensive_deadband(label, risk_net, prior_label, audit=audit)


def _apply_tier2_forced(
    label: str, b_forced: str | None, recovery_forced: str | None
) -> str:
    """Apply Tier-2 forced label; recovery path may de-escalate below mapped risk label."""
    effective = recovery_forced if recovery_forced is not None else b_forced
    if effective is None:
        return label
    if (
        b_forced
        and recovery_forced
        and _SEVERITY[recovery_forced] < _SEVERITY[b_forced]
    ):
        return effective if _SEVERITY[effective] < _SEVERITY[label] else label
    return _escalate(label, effective)


def _apply_ceiling(label: str, ceiling: str | None) -> str:
    """Layer-A / over-extension ceiling caps the *constructive* side only (never blocks
    downgrades). ``hold`` caps buy/accumulate -> hold; ``accumulate`` caps buy -> accumulate.
    """
    if ceiling == "hold" and label in ("buy", "accumulate"):
        return "hold"
    if ceiling == "accumulate" and label == "buy":
        return "accumulate"
    return label


# STEP 2b: over-extension ceilings on the literal buy/accumulate labels. A statistically
# stretched name (high ATR-normalized EMA200 stretch, high z, far above EMA200, pinned to
# its 20d high) should not earn a *fresh* buy even when the risk score is low; cap the
# constructive label. Shadow-first: default mode records the would-be ceiling without
# changing the label. NaN inputs simply don't contribute (degrades to no ceiling).
_SIGV2_CEIL_STRETCH_ACCUM = 4.0    # ATR stretch -> at least accumulate ceiling
_SIGV2_CEIL_STRETCH_HOLD = 6.0     # ATR stretch -> hold ceiling
_SIGV2_CEIL_Z_HOT = 2.5            # upside z that corroborates over-extension
_SIGV2_CEIL_EMA_DIST_HOT = 18.0    # % above EMA200 that corroborates
_SIGV2_CEIL_NEAR_HIGH_PCT = 1.0    # within this % of the 20d high = pinned to high


def _overextension_ceiling_mode() -> str:
    raw = os.environ.get("TITAN_SIGV2_OVEREXT_CEILING_MODE", "").strip().lower()
    return raw if raw in ("off", "shadow", "damp", "enforce") else "shadow"


def _overextension_ceiling(audit: dict[str, Any]) -> dict[str, Any]:
    """Return {ceiling, hot, reasons, components} for the over-extension label cap."""
    stretch = _sf(audit.get("ema200_stretch_atr"))
    z = _sf(audit.get("z_score"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    to_high = _sf(audit.get("breakout_20d_distance_pct_to_high"))

    s_accum = _env_float("TITAN_SIGV2_CEIL_STRETCH_ACCUM", _SIGV2_CEIL_STRETCH_ACCUM)
    s_hold = _env_float("TITAN_SIGV2_CEIL_STRETCH_HOLD", _SIGV2_CEIL_STRETCH_HOLD)
    z_hot = _env_float("TITAN_SIGV2_CEIL_Z_HOT", _SIGV2_CEIL_Z_HOT)
    ema_hot = _env_float("TITAN_SIGV2_CEIL_EMA_DIST_HOT", _SIGV2_CEIL_EMA_DIST_HOT)
    near_high_pct = _env_float("TITAN_SIGV2_CEIL_NEAR_HIGH_PCT", _SIGV2_CEIL_NEAR_HIGH_PCT)

    hot: list[str] = []
    if not math.isnan(stretch) and stretch >= s_accum:
        hot.append(f"stretch {stretch:.2f}")
    if not math.isnan(z) and z >= z_hot:
        hot.append(f"z {z:.2f}")
    if not math.isnan(ema_dist) and ema_dist >= ema_hot:
        hot.append(f"ema_dist {ema_dist:.1f}%")
    if not math.isnan(to_high) and to_high >= -abs(near_high_pct):
        hot.append("pinned to 20d-high")

    ceiling: str | None = None
    extreme_stretch = (not math.isnan(stretch)) and stretch >= s_hold
    if extreme_stretch or len(hot) >= 3:
        ceiling = "hold"
    elif (not math.isnan(stretch) and stretch >= s_accum) or len(hot) >= 2:
        ceiling = "accumulate"
    return {
        "ceiling": ceiling,
        "hot": hot,
        "components": {
            "ema200_stretch_atr": _round(stretch),
            "z_score": _round(z),
            "ema_200_distance_pct": _round(ema_dist),
            "breakout_20d_distance_pct_to_high": _round(to_high),
        },
    }


def _combine_ceilings(*ceilings: str | None) -> str | None:
    """Most restrictive of several ceilings (hold > accumulate > none)."""
    rank = {None: 0, "accumulate": 1, "hold": 2}
    best = None
    for c in ceilings:
        if rank.get(c, 0) > rank.get(best, 0):
            best = c
    return best


# Phase 2: next-open gap entry guard (shadow-first). Until live opening prints exist,
# derive gap from explicit stored fields, return-series OHLC, or next-session session_move.
_SIGV2_GAP_UP_PCT = 2.5
_SIGV2_GAP_UP_HOLD_PCT = 5.0
_SIGV2_GAP_DOWN_PCT = -1.5
_SIGV2_GAP_DOWN_TRIM_PCT = -3.0


def _gap_guard_mode() -> str:
    raw = os.environ.get("TITAN_GAP_GUARD_MODE", "").strip().lower()
    return raw if raw in ("shadow", "damp", "skip") else "shadow"


def _gap_from_return_series(audit: dict[str, Any]) -> tuple[float, str]:
    """Gap from the last two OHLC sessions: next open vs prior close."""
    for key in ("return_series", "ohlc_sessions", "ohlc_return_series"):
        raw = audit.get(key)
        if not isinstance(raw, list) or len(raw) < 2:
            continue
        prev = raw[-2]
        nxt = raw[-1]
        if not isinstance(prev, dict) or not isinstance(nxt, dict):
            continue
        prev_close = _sf(
            prev.get("close")
            if prev.get("close") is not None
            else prev.get("close_last")
        )
        next_open = _sf(
            nxt.get("open") if nxt.get("open") is not None else nxt.get("open_last")
        )
        if (
            not math.isnan(prev_close)
            and prev_close != 0.0
            and not math.isnan(next_open)
        ):
            return ((next_open / prev_close) - 1.0) * 100.0, key
    return float("nan"), "none"


def _derive_next_open_gap_pct(audit: dict[str, Any]) -> tuple[float, str]:
    """Return (gap_pct, source). NaN gap => guard is a no-op."""
    for key in (
        "next_open_gap_pct",
        "next_session_gap_pct",
        "next_open_vs_signal_close_pct",
        "open_gap_pct",
    ):
        v = _sf(audit.get(key))
        if not math.isnan(v):
            return v, key

    signal_close = _sf(audit.get("signal_close_last"))
    if math.isnan(signal_close):
        signal_close = _sf(audit.get("close_last"))
    for open_key in ("next_session_open", "next_open", "open_next"):
        nxt_open = _sf(audit.get(open_key))
        if (
            not math.isnan(signal_close)
            and signal_close != 0.0
            and not math.isnan(nxt_open)
        ):
            return ((nxt_open / signal_close) - 1.0) * 100.0, open_key

    gap_pct, src = _gap_from_return_series(audit)
    if not math.isnan(gap_pct):
        return gap_pct, src

    for key in ("next_session_move_vs_prev_close_pct",):
        v = _sf(audit.get(key))
        if not math.isnan(v):
            return v, key

    if bool(audit.get("gap_guard_next_open_proxy")):
        v = _sf(audit.get("session_move_vs_prev_close_pct"))
        if not math.isnan(v):
            return v, "session_move_vs_prev_close_pct"

    return float("nan"), "none"


def _gap_guard(audit: dict[str, Any], label: str) -> dict[str, Any]:
    """Shadow-first next-open gap guard for constructive entry labels."""
    gap_pct, source = _derive_next_open_gap_pct(audit)
    up_pct = _env_float("TITAN_GAP_GUARD_UP_PCT", _SIGV2_GAP_UP_PCT)
    up_hold = _env_float("TITAN_GAP_GUARD_UP_HOLD_PCT", _SIGV2_GAP_UP_HOLD_PCT)
    down_pct = _env_float("TITAN_GAP_GUARD_DOWN_PCT", _SIGV2_GAP_DOWN_PCT)
    down_trim = _env_float("TITAN_GAP_GUARD_DOWN_TRIM_PCT", _SIGV2_GAP_DOWN_TRIM_PCT)

    out: dict[str, Any] = {
        "gap_pct": None if math.isnan(gap_pct) else _round(gap_pct),
        "source": source,
        "would_action": None,
        "would_ceiling": None,
        "would_forced_label": None,
        "reason": "no_gap_data",
    }

    if math.isnan(gap_pct):
        return out
    if label not in ("buy", "accumulate"):
        out["reason"] = "not_constructive"
        return out

    if gap_pct >= up_pct:
        ceiling = "hold" if gap_pct >= up_hold else "accumulate"
        out.update(
            {
                "would_action": "damp",
                "would_ceiling": ceiling,
                "reason": f"gap_up {gap_pct:.2f}% >= {up_pct}%",
            }
        )
        return out

    if gap_pct <= down_pct:
        forced = "trim" if gap_pct <= down_trim else "hold"
        out.update(
            {
                "would_action": "skip",
                "would_forced_label": forced,
                "reason": f"gap_down {gap_pct:.2f}% <= {down_pct}%",
            }
        )
        return out

    out["reason"] = f"gap {gap_pct:.2f}% within band"
    return out


def _escalate(label: str, forced: str | None) -> str:
    if forced is None:
        return label
    return forced if _SEVERITY[forced] > _SEVERITY[label] else label


# Recovery / rally de-escalation defaults (override via TITAN_SIGV2_* env).
_RECOVERY_INTENT_MIN = 60.0
_RECOVERY_NW_MIN = 55.0
_RALLY_INTENT_MIN = 65.0
_RALLY_NW_MIN = 60.0
_PARTICIPATION_INTENT_MIN = 65.0
_PARTICIPATION_NW_MIN = 62.0
_PARTICIPATION_VPR_MIN = 1.2
_ACCUM_NEXT_WEEK_MIN = 58.0
_ACCUM_INTENT_MIN = 58.0
_THIN_LIQ_INTENT_MIN = 65.0
_THIN_LIQ_NW_MIN = 60.0
_RECOVERY_RISK_MAX = 5.0
# Tier-2 post-overextension / intent-led de-escalation (tunable via TITAN_SIGV2_TIER2_* env).
_TIER2_RECOVERY_INTENT_MIN = 65.0
_TIER2_RECOVERY_NW_MIN = 55.0
_TIER2_RECOVERY_RISK_MAX = 5.0
_TIER2_INTENT_LED_INTENT_MIN = 70.0
_TIER2_INTENT_LED_VPR_MIN = 1.2


def _prior_defensive_streak(audit: dict[str, Any]) -> int:
    """Count consecutive prior sessions at trim/exit-risk (when history is wired)."""
    streak = 0
    for key in ("prev_action_signal", "prev_prev_action_signal"):
        prior = str(audit.get(key) or "").strip().lower()
        if prior in ("trim", "exit-risk"):
            streak += 1
        elif prior:
            break
    return streak


def _recovery_tape_ok(audit: dict[str, Any], risk_net: float, *, rally: bool) -> bool:
    risk_max = _env_float("TITAN_SIGV2_RECOVERY_RISK_MAX", _RECOVERY_RISK_MAX)
    if risk_net >= risk_max:
        return False
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    if rally:
        intent_min = _env_float("TITAN_SIGV2_RALLY_INTENT_MIN", _RALLY_INTENT_MIN)
        nw_min = _env_float("TITAN_SIGV2_RALLY_NW_MIN", _RALLY_NW_MIN)
        ret5d = _sf(audit.get("return_5d_pct"))
        if math.isnan(ret5d) or ret5d <= 0.0:
            return False
    else:
        intent_min = _env_float("TITAN_SIGV2_RECOVERY_INTENT_MIN", _RECOVERY_INTENT_MIN)
        nw_min = _env_float("TITAN_SIGV2_RECOVERY_NW_MIN", _RECOVERY_NW_MIN)
    if math.isnan(eff) or eff < intent_min or math.isnan(nw) or nw < nw_min:
        if rally:
            return False
        prev_risk = _sf(audit.get("prev_risk_net"))
        return not math.isnan(prev_risk) and risk_net < prev_risk - 0.25
    return True


def _tier2_overext_primary(c: dict[str, Any], b_reasons: list[str] | None = None) -> bool:
    """True when stretch/over-extension was a Tier-2 corroborator."""
    if bool(c.get("over_extension_hot")):
        return True
    if b_reasons:
        return any("over-extension hot" in r for r in b_reasons)
    return False


def _tier2_risk_ok_for_recovery(audit: dict[str, Any], risk_net: float) -> bool:
    """Tier-2 recovery allows risk_net below ceiling or clearly dropping vs prior."""
    risk_max = _env_float("TITAN_SIGV2_TIER2_RECOVERY_RISK_MAX", _TIER2_RECOVERY_RISK_MAX)
    if risk_net < risk_max:
        return True
    prev_risk = _sf(audit.get("prev_risk_net"))
    return not math.isnan(prev_risk) and risk_net < prev_risk - 0.25


def _tier2_tape_rally_ok(audit: dict[str, Any]) -> bool:
    """Same-day or trailing-week rally tape for Tier-2 de-escalation."""
    ret1d = _sf(audit.get("return_1d_pct"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ret1d_min = _env_float("TITAN_SIGV2_TIER2_RECOVERY_RET1D_MIN", 0.0)
    return (not math.isnan(ret5d) and ret5d > 0.0) or (
        not math.isnan(ret1d) and ret1d > ret1d_min
    )


def _tier2_post_overext_recovery_ok(
    audit: dict[str, Any], risk_net: float, *, c: dict[str, Any], b_reasons: list[str]
) -> bool:
    """Post-overextension trim/exit when intent/nw/5d rally recovered (risk still elevated)."""
    if not _tier2_overext_primary(c, b_reasons):
        return False
    if not _tier2_risk_ok_for_recovery(audit, risk_net):
        return False
    if not _tier2_tape_rally_ok(audit):
        return False
    intent_min = _env_float("TITAN_SIGV2_TIER2_RECOVERY_INTENT_MIN", _TIER2_RECOVERY_INTENT_MIN)
    nw_min = _env_float("TITAN_SIGV2_TIER2_RECOVERY_NW_MIN", _TIER2_RECOVERY_NW_MIN)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(nw)
        and nw >= nw_min
    )


def _tier2_intent_led_recovery_ok(audit: dict[str, Any], risk_net: float) -> bool:
    """Intent-led Tier-2 cap: prior defensive + strong intent + rising VPR on rally tape."""
    prior = str(audit.get("prev_action_signal") or "").strip().lower()
    if prior not in ("trim", "exit-risk"):
        return False
    if not _tier2_risk_ok_for_recovery(audit, risk_net):
        return False
    if not _tier2_tape_rally_ok(audit):
        return False
    intent_min = _env_float("TITAN_SIGV2_TIER2_INTENT_LED_INTENT_MIN", _TIER2_INTENT_LED_INTENT_MIN)
    vpr_min = _env_float("TITAN_SIGV2_TIER2_INTENT_LED_VPR_MIN", _TIER2_INTENT_LED_VPR_MIN)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    vpr = _participation_vpr(audit)
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
    )


def _tier2_recovery_constructive_cap(
    gate: dict[str, bool],
    audit: dict[str, Any],
    risk_net: float,
    *,
    buy_allowed: bool,
) -> str:
    """Tier-2 recovery cap: accumulate when scores/participation strong despite elevated risk."""
    if gate.get("constructive_core"):
        return "accumulate"
    risk_max = _env_float("TITAN_SIGV2_TIER2_RECOVERY_RISK_MAX", _TIER2_RECOVERY_RISK_MAX)
    intent_min = _env_float("TITAN_SIGV2_TIER2_INTENT_LED_INTENT_MIN", _TIER2_INTENT_LED_INTENT_MIN)
    vpr_min = _env_float("TITAN_SIGV2_TIER2_INTENT_LED_VPR_MIN", _TIER2_INTENT_LED_VPR_MIN)
    nw_min = _env_float("TITAN_SIGV2_TIER2_RECOVERY_NW_MIN", _TIER2_RECOVERY_NW_MIN)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    vpr = _participation_vpr(audit)
    if (
        buy_allowed
        and risk_net < risk_max
        and not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(nw)
        and nw >= nw_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
    ):
        return "accumulate"
    if (
        buy_allowed
        and gate.get("constructive_scores")
        and _accumulate_band(audit, risk_net, buy_allowed=buy_allowed)
    ):
        return "accumulate"
    return "hold"


def _recovery_constructive_cap(
    gate: dict[str, bool],
    audit: dict[str, Any],
    risk_net: float,
    *,
    buy_allowed: bool,
) -> str:
    if gate.get("constructive_core"):
        return "accumulate"
    if (
        buy_allowed
        and gate.get("constructive_scores")
        and _accumulate_band(audit, risk_net, buy_allowed=buy_allowed)
    ):
        return "accumulate"
    if _participation_accumulate_ok(audit, risk_net, buy_allowed=buy_allowed):
        return "accumulate"
    return "hold"


def _participation_accumulate_ok(
    audit: dict[str, Any], risk_net: float, *, buy_allowed: bool
) -> bool:
    """DATAMATICS-class: strong intent + decent horizon + supportive VPR → accumulate."""
    if not buy_allowed or risk_net >= _buy_risk_ceiling():
        return False
    intent_min = _env_float("TITAN_SIGV2_PARTICIPATION_INTENT_MIN", _PARTICIPATION_INTENT_MIN)
    nw_min = _env_float("TITAN_SIGV2_PARTICIPATION_NW_MIN", _PARTICIPATION_NW_MIN)
    vpr_min = _env_float("TITAN_SIGV2_PARTICIPATION_VPR_MIN", _PARTICIPATION_VPR_MIN)
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    nw = _sf(audit.get("next_week_score"))
    vpr = _participation_vpr(audit)
    return (
        not math.isnan(eff)
        and eff >= intent_min
        and not math.isnan(nw)
        and nw >= nw_min
        and not math.isnan(vpr)
        and vpr >= vpr_min
    )


def _cap_tier2_for_recovery(
    forced_label: str | None,
    *,
    audit: dict[str, Any],
    risk_net: float,
    bypass: bool,
    corroborators: int,
    gate: dict[str, bool],
    buy_allowed: bool,
    c: dict[str, Any] | None = None,
    b_reasons: list[str] | None = None,
    staleflow_downgrade: bool = False,
) -> str | None:
    """Cap Tier-2 forced trim/exit when intent/nw recovered (Tier-1 bypass untouched)."""
    if bypass or forced_label is None:
        return forced_label
    c = c or {}
    b_reasons = b_reasons or []
    exit_count = _env_int("TITAN_SIGV2_B_TIER2_EXIT_COUNT", 3)
    trim_count = _env_int("TITAN_SIGV2_B_TIER2_TRIM_COUNT", 2)
    rally_ok = _recovery_tape_ok(audit, risk_net, rally=True)
    recovery_ok = rally_ok or _recovery_tape_ok(audit, risk_net, rally=False)
    if staleflow_downgrade and not rally_ok:
        return forced_label
    tier2_overext_ok = _tier2_post_overext_recovery_ok(audit, risk_net, c=c, b_reasons=b_reasons)
    tier2_intent_ok = _tier2_intent_led_recovery_ok(audit, risk_net)
    if tier2_overext_ok or tier2_intent_ok:
        cap = _tier2_recovery_constructive_cap(gate, audit, risk_net, buy_allowed=buy_allowed)
        if _SEVERITY[cap] < _SEVERITY.get(forced_label, 0):
            audit["tier2_recovery_deescalation"] = True
            return cap
        return forced_label
    if rally_ok:
        cap = _recovery_constructive_cap(gate, audit, risk_net, buy_allowed=buy_allowed)
        if _SEVERITY[cap] < _SEVERITY.get(forced_label, 0):
            return cap
        return forced_label
    if not recovery_ok:
        return forced_label
    if forced_label == "exit-risk" and corroborators < exit_count:
        cap = _recovery_constructive_cap(gate, audit, risk_net, buy_allowed=buy_allowed)
        return cap if _SEVERITY[cap] < _SEVERITY[forced_label] else forced_label
    if forced_label == "trim":
        if corroborators < trim_count and _prior_defensive_streak(audit) < 2:
            return None
        cap = _recovery_constructive_cap(gate, audit, risk_net, buy_allowed=buy_allowed)
        return cap if _SEVERITY[cap] < _SEVERITY[forced_label] else forced_label
    return forced_label


def _apply_hysteresis(
    label: str,
    risk_net: float,
    *,
    prior_label: str | None,
    bypass: bool,
    buffer: float,
    audit: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Asymmetric hysteresis with expanded entry/exit deadbands.

    Enter buy/accumulate: risk_net < buy_ceiling (default 3.0).
    Enter trim: risk_net >= trim_floor (default 5.0).
    From trim/exit-risk: cannot upgrade to constructive until risk_net < buy_ceiling.
    """
    if audit is None:
        audit = {}
    buy_max = _buy_risk_ceiling()
    trim_min = _trim_risk_floor()
    if prior_label is None or prior_label == label:
        return label, False
    if bypass or label == "exit-risk":
        return label, False  # fast out on genuine danger

    rally_ok = _recovery_tape_ok(audit, risk_net, rally=True)
    recovery_ok = rally_ok or _recovery_tape_ok(audit, risk_net, rally=False)
    if audit.get("tier2_recovery_deescalation"):
        recovery_ok = True

    # From defensive labels: require strict buy ceiling before constructive upgrade.
    if prior_label in ("trim", "exit-risk") and label in ("hold", "accumulate", "buy"):
        if label == "buy" and risk_net >= buy_max:
            return "hold", True
        if buy_max <= risk_net < trim_min:
            if label in ("buy", "accumulate") and not recovery_ok:
                return "hold", True
            if label == "buy":
                return "hold", True
            if not recovery_ok:
                return prior_label, True
            return label, False
        if risk_net >= buy_max:
            if label in ("buy", "accumulate"):
                return "hold", True
            if not recovery_ok:
                return prior_label, True
            return label, False
        if recovery_ok:
            return label, False
        return prior_label, True

    if prior_label == "trim" and label == "hold" and not recovery_ok:
        return prior_label, True

    exit_count = _env_int("TITAN_SIGV2_B_TIER2_EXIT_COUNT", 3)
    if (
        prior_label in ("hold", "accumulate", "buy")
        and label == "trim"
        and risk_net < trim_min
        and recovery_ok
        and _prior_defensive_streak(audit) < 2
    ):
        return prior_label, True

    # Hold<->trim churn: trim only sticks when risk clears trim floor.
    prior_trim = prior_label == "trim"
    label_trim = label == "trim"
    prior_hold = prior_label == "hold"
    label_hold = label == "hold"
    if (prior_trim and label_hold) or (prior_hold and label_trim):
        if label_trim and risk_net < trim_min:
            if recovery_ok and _prior_defensive_streak(audit) < 2:
                return prior_label, True
            if not recovery_ok:
                return prior_label, True
            return label, False
        if label_hold and risk_net >= trim_min - buffer and not recovery_ok:
            return prior_label, True
        return label, False

    # Entering constructive labels requires margin below the buy ceiling.
    if label in ("buy", "accumulate") and risk_net >= buy_max - buffer:
        if audit.get("tier2_recovery_deescalation"):
            return label, False
        return prior_label, True

    return label, False


def _confidence(
    *,
    final_label: str,
    risk_net: float,
    bull_c: float,
    trace: list[dict[str, Any]],
    corroborators: int,
    seed: float,
    buy_cap: float | None,
    bypass: bool,
) -> float:
    if bypass:
        return _round(_clamp(max(0.9, seed), 0.0, 1.0), 3)
    dominant_bear = final_label in ("trim", "exit-risk") or risk_net >= _trim_risk_floor()
    if dominant_bear:
        n = sum(1 for t in trace if t.get("side") == "bear" and float(t.get("points") or 0) > 0.05)
        n += corroborators
    else:
        n = sum(1 for t in trace if t.get("side") == "bull" and float(t.get("points") or 0) > 0.05)
    base = _clamp(0.4 + 0.1 * n, 0.0, 0.95)
    conf = base * seed
    if final_label in ("buy", "accumulate") and buy_cap is not None:
        conf = min(conf, buy_cap)
    return _round(_clamp(conf, 0.0, 1.0), 3)


def evaluate_signal_v2(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    """Run the A-E waterfall. Returns (label, risk_net, reasons) for backward-compat,
    and writes ``signal_confidence`` / ``signal_reason_trace`` / ``signal_engine_version``
    onto the audit dict in place.
    """
    a = layer_a(audit)
    c = layer_c(audit)
    d = layer_d(audit, c)
    b = layer_b(audit, c, d)

    agg = _aggregate(c, d, audit)
    risk_c = agg["risk_c"]
    bull_c = agg["bull_c"]

    bull_offset = _env_float("TITAN_SIGV2_E_BULL_OFFSET", 0.5)
    buffer = _env_float("TITAN_SIGV2_E_HYST_BUFFER", 0.5)

    risk_net = _clamp(risk_c - bull_offset * bull_c, 0.0, 10.0)

    gate = _buy_gate(audit, a, c)
    prior_label = _prior_label_from_audit(audit)
    label = _map_label(risk_net, gate, audit, a, prior_label=prior_label)
    if bool(audit.get("history_lt_200_sessions")):
        if _ipo_leader_buy_ok(audit, risk_net) and gate.get("clean_buy"):
            label = "buy"
            a["label_ceiling"] = None
            audit["ipo_leader_exception"] = {"applied_buy": True}
        elif label in ("buy", "accumulate") and not _short_history_accumulate_ok(audit, risk_net):
            label = "hold"
    # STEP 2b: over-extension ceiling (shadow-first). Compute the would-be cap always;
    # apply it only when enforced (damp applies the milder accumulate cap, never hold).
    oe_mode = _overextension_ceiling_mode()
    oe = _overextension_ceiling(audit)
    oe_ceiling = oe.get("ceiling")
    applied_oe_ceiling: str | None = None
    if oe_mode == "enforce":
        applied_oe_ceiling = oe_ceiling
    elif oe_mode == "damp" and oe_ceiling is not None:
        applied_oe_ceiling = "accumulate"
    label_before_oe = label
    label = _apply_ceiling(label, _combine_ceilings(a.get("label_ceiling"), applied_oe_ceiling))
    audit["signal_overext_ceiling"] = {
        "mode": oe_mode,
        "would_ceiling": oe_ceiling,
        "applied_ceiling": applied_oe_ceiling,
        "label_before": label_before_oe,
        "label_after": label,
        "hot": oe.get("hot", []),
        "components": oe.get("components", {}),
    }
    # Phase 2: next-open gap guard (shadow-first). Logs would_action always; applies ceiling /
    # escalation only in damp / skip enforcement modes.
    gg_mode = _gap_guard_mode()
    gg = _gap_guard(audit, label)
    applied_gg_ceiling: str | None = None
    applied_gg_forced: str | None = None
    if gg_mode == "damp" and gg.get("would_action") == "damp":
        applied_gg_ceiling = gg.get("would_ceiling")
    elif gg_mode == "skip" and gg.get("would_action") == "skip":
        applied_gg_forced = gg.get("would_forced_label")
    label_before_gg = label
    label = _apply_ceiling(label, applied_gg_ceiling)
    label = _escalate(label, applied_gg_forced)
    audit["gap_guard"] = {
        "mode": gg_mode,
        "gap_pct": gg.get("gap_pct"),
        "source": gg.get("source"),
        "would_action": gg.get("would_action"),
        "would_ceiling": gg.get("would_ceiling"),
        "would_forced_label": gg.get("would_forced_label"),
        "applied_ceiling": applied_gg_ceiling,
        "applied_forced_label": applied_gg_forced,
        "label_before": label_before_gg,
        "label_after": label,
        "reason": gg.get("reason"),
    }
    forced_label = _cap_tier2_for_recovery(
        b.get("forced_label"),
        audit=audit,
        risk_net=risk_net,
        bypass=bool(b.get("bypass_hysteresis")),
        corroborators=int(b.get("corroborators", 0)),
        gate=gate,
        buy_allowed=_constructive_allowed(a, audit),
        c=c,
        b_reasons=b.get("reasons", []),
        staleflow_downgrade=bool(d.get("staleflow_downgrade")),
    )
    label = _apply_tier2_forced(label, b.get("forced_label"), forced_label)
    label = _apply_prior_defensive_deadband(label, risk_net, prior_label, audit=audit)

    bypass = bool(b.get("bypass_hysteresis"))
    label, _hyst = _apply_hysteresis(
        label,
        risk_net,
        prior_label=prior_label,
        bypass=bypass,
        buffer=buffer,
        audit=audit,
    )

    confidence = _confidence(
        final_label=label,
        risk_net=risk_net,
        bull_c=bull_c,
        trace=c.get("trace", []),
        corroborators=int(b.get("corroborators", 0)),
        seed=float(a.get("confidence_seed", 1.0)),
        buy_cap=d.get("buy_confidence_cap"),
        bypass=bypass,
    )

    # Assemble human-readable reasons (replaces legacy 4-reason truncation).
    reasons: list[str] = []
    reasons.extend(b.get("reasons", []))
    reasons.extend(d.get("reasons", []))
    for t in c.get("trace", []):
        reasons.append(f"{t['group']} {t['metric']}={t['value']} ({t['side']} {t['points']})")
    reasons.extend(a.get("reasons", []))
    if not reasons:
        reasons = ["v2: technical+risk profile stable"]

    audit["signal_confidence"] = confidence
    audit["signal_reason_trace"] = {
        "risk_c": _round(risk_c),
        "bull_c": _round(bull_c),
        "risk_net": _round(risk_net),
        "corroborators": int(b.get("corroborators", 0)),
        "forced_label": b.get("forced_label"),
        "terms": c.get("trace", []),
        "modifiers": d.get("reasons", []),
        "data_quality": a.get("reasons", []),
        "adx_regime_mults": d.get("adx_regime_mults"),
    }
    audit["signal_engine_version"] = "v2"

    return label, round(risk_net, 2), reasons[:8]
