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


def v2_enabled() -> bool:
    """V2 is always the production signal path."""
    return True


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
    "return_10d_pct",
)

_SEVERITY: dict[str, int] = {
    "buy": 0,
    "accumulate": 1,
    "hold": 2,
    "trim": 3,
    "exit-risk": 4,
}


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
        out["buy_allowed"] = False
        out["label_ceiling"] = "hold"  # short history cannot reach accumulate/buy
        seed *= short_hist_conf
        reasons.append("short history (<200 sessions): ceiling=hold")

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
    ret1d = _sf(audit.get("return_1d_pct"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ret10d = _sf(audit.get("return_10d_pct"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    atr_pct = _sf(audit.get("atr_14_pct"))
    atr_pi = _sf(audit.get("atr_penalty_input"))
    extreme_move = bool(audit.get("extreme_price_move_proxy"))
    ret1d_weight = 0.45 if extreme_move else 1.0

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

    # Multi-day momentum (cap 3): ramped sub-terms summed then capped
    mom = 0.0
    mom += _ramp(ret1d, zero_at=-1.0, full_at=-2.0, full_points=2.0 * ret1d_weight)
    mom += _ramp(ret5d, zero_at=-2.0, full_at=-6.0, full_points=2.0)
    mom += _ramp(ret10d, zero_at=-6.0, full_at=-10.0, full_points=1.5)
    fam["momentum"] = min(3.0, mom)
    add("momentum", "return_1d/5d/10d_pct", ret1d, fam["momentum"])

    # Below-EMA200 trend only (cap 2): ramp -2 -> -6 (over-extension is C-8)
    em = _ramp(ema_dist, zero_at=-2.0, full_at=-6.0, full_points=2.0)
    fam["trend"] = min(2.0, em)
    add("trend", "ema_200_distance_pct", ema_dist, fam["trend"])

    # Volatility (cap 2): prefer ATR-vs-sector input, else raw ATR%
    if not math.isnan(atr_pi):
        vo = _ramp(atr_pi, zero_at=1.25, full_at=2.2, full_points=2.0)
        vmetric, vval = "atr_penalty_input", atr_pi
    else:
        vo = _ramp(atr_pct, zero_at=4.0, full_at=6.0, full_points=2.0)
        vmetric, vval = "atr_14_pct", atr_pct
    fam["volatility"] = min(2.0, vo)
    add("volatility", vmetric, vval, fam["volatility"])

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
    obv = _sf(audit.get("obv_slope_20"))
    stretch = _sf(audit.get("ema200_stretch_atr"))
    stretch_pctile = _sf(audit.get("sector_pctile_ema200_stretch"))
    z = _sf(audit.get("z_score"))

    k_cmf = _env_float("TITAN_SIGV2_C_CMF_K", 10.0)
    cap_cmf = _env_float("TITAN_SIGV2_C_CMF_CAP", 2.0)
    # Fix A (mirror): C-8 over-extension is now more sensitive. Default deadband
    # lowered 4.0 -> 3.0 ATR (tunable down to ~2.0-2.5 via env).
    stretch_deadband = _env_float("TITAN_SIGV2_C_STRETCH_DEADBAND_ATR", 3.0)
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
            # OBV only AMPLIFIES an existing same-sign (negative) cmf term.
            if money_flow_bear > 0.0 and not math.isnan(obv) and obv < 0.0:
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
            if money_flow_bull > 0.0 and not math.isnan(obv) and obv > 0.0:
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
        "divergence_bump": 0.0,
        "buy_confidence_cap": None,
        "pullback_bull_bump": 0.0,
        "staleflow_downgrade": False,
        "reasons": [],
    }

    reasons: list[str] = []
    adx = _sf(audit.get("adx_14"))
    cmf = _sf(audit.get("cmf_20"))
    obv = _sf(audit.get("obv_slope_20"))
    ret1d = _sf(audit.get("return_1d_pct"))
    ret5d = _sf(audit.get("return_5d_pct"))
    ema_dist = _sf(audit.get("ema_200_distance_pct"))
    vpr = _sf(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))

    adx_weak = _env_float("TITAN_SIGV2_D_ADX_WEAK", 20.0)
    adx_strong = _env_float("TITAN_SIGV2_D_ADX_STRONG", 25.0)
    divergence_ret1d = _env_float("TITAN_SIGV2_D_DIVERGENCE_RET1D", 2.0)
    pullback_vpr = _env_float("TITAN_SIGV2_D_PULLBACK_VPR", 1.0)
    staleflow_eps = _env_float("TITAN_SIGV2_D_STALEFLOW_OBV_EPS", 0.0)

    # 1) ADX regime: weak trend up-weights mean-reversion (cmf/over-extension),
    #    down-weights momentum; strong trend does the inverse.
    if not math.isnan(adx):
        if adx < adx_weak:
            out["mult_money_flow"] = 1.3
            out["mult_over_extension"] = 1.3
            out["mult_momentum"] = 0.7
            reasons.append(f"weak ADX {adx:.1f}: mean-reversion up-weighted")
        elif adx >= adx_strong:
            out["mult_money_flow"] = 0.7
            out["mult_over_extension"] = 0.7
            out["mult_momentum"] = 1.3
            reasons.append(f"strong ADX {adx:.1f}: momentum up-weighted")

    # 2) Money-flow divergence ("hollow breakout"): price up on net distribution.
    if (not math.isnan(ret1d) and ret1d > divergence_ret1d) and (not math.isnan(cmf) and cmf < -0.05):
        out["divergence_bump"] = 1.0
        out["buy_confidence_cap"] = 0.5
        reasons.append(f"hollow-breakout divergence (ret1d {ret1d:.2f}%, cmf {cmf:.3f})")

    # 3) Healthy-pullback rescue: low-volume down day with intact flow & trend.
    if (
        (not math.isnan(ret1d) and ret1d < 0.0)
        and (not math.isnan(vpr) and vpr < pullback_vpr)
        and (not math.isnan(cmf) and cmf > 0.05)
        and (not math.isnan(ret5d) and ret5d >= -3.0)
        and (not math.isnan(ema_dist) and ema_dist >= 0.0)
    ):
        out["mult_momentum"] = min(out["mult_momentum"], 0.5)
        out["pullback_bull_bump"] = 0.5
        reasons.append("healthy low-volume pullback: momentum penalty halved")

    # 4) Stale-flow OBV tiebreaker (GREAVESCOT rule): neutral flow + over-extended +
    #    weak trend + flat/negative OBV -> corroborated downgrade (forces TRIM).
    if (
        (not math.isnan(cmf) and -0.05 <= cmf <= 0.05)
        and bool(c.get("over_extension_hot"))
        and (not math.isnan(adx) and adx < adx_weak)
        and (not math.isnan(obv) and obv <= staleflow_eps)
    ):
        out["staleflow_downgrade"] = True
        reasons.append("stale-flow: neutral CMF + over-extended + weak ADX + flat/neg OBV")

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
    trim_count = _env_int("TITAN_SIGV2_B_TIER2_TRIM_COUNT", 2)
    exit_count = _env_int("TITAN_SIGV2_B_TIER2_EXIT_COUNT", 3)

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
    if bool(c.get("over_extension_hot")):
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
    out["corroborators"] = count
    if count >= exit_count:
        out["forced_label"] = "exit-risk"
        reasons.append(f"Tier-2: {count} corroborators -> exit-risk ({', '.join(signals)})")
    elif count >= trim_count or bool(d.get("staleflow_downgrade")):
        out["forced_label"] = "trim"
        reasons.append(f"Tier-2: {count} corroborators -> trim ({', '.join(signals)})")
    out["reasons"] = reasons
    return out


# ---------------------------------------------------------------------------
# Layer E: aggregation, mapping, confidence, hysteresis
# ---------------------------------------------------------------------------


def _aggregate(c: dict[str, Any], d: dict[str, Any]) -> dict[str, float]:
    """Apply Layer-D multipliers + bumps to Layer-C terms -> risk_c, bull_c."""
    fam = c.get("families", {})
    mult_mom = float(d.get("mult_momentum", 1.0))
    mult_mf = float(d.get("mult_money_flow", 1.0))
    mult_oe = float(d.get("mult_over_extension", 1.0))

    risk_c = 0.0
    risk_c += float(fam.get("horizon", 0.0))
    risk_c += float(fam.get("intent", 0.0))
    risk_c += float(fam.get("z", 0.0))
    risk_c += min(3.0, float(fam.get("momentum", 0.0)) * mult_mom)
    risk_c += float(fam.get("trend", 0.0))
    risk_c += float(fam.get("volatility", 0.0))
    risk_c += float(c.get("money_flow_bear", 0.0)) * mult_mf
    risk_c += float(c.get("over_extension", 0.0)) * mult_oe
    risk_c += float(c.get("upside_z", 0.0)) * mult_oe
    risk_c += float(c.get("fundamental", 0.0))  # may be negative (strong fundamentals)
    risk_c += float(d.get("divergence_bump", 0.0))
    risk_c = _clamp(risk_c, 0.0, 10.0)

    bull_c = float(c.get("money_flow_bull", 0.0)) * mult_mf
    bull_c += float(d.get("pullback_bull_bump", 0.0))
    bull_c = _clamp(bull_c, 0.0, 10.0)
    return {"risk_c": risk_c, "bull_c": bull_c}


def _buy_gate(audit: dict[str, Any], a: dict[str, Any], c: dict[str, Any]) -> dict[str, bool]:
    """Ported legacy BUY gate + money-flow / over-extension constructive checks.

    Returns clean_buy (all constructive incl. flow & not over-extended) and
    constructive_core (everything except the flow / over-extension checks).
    """
    next_week = _sf(audit.get("next_week_score"))
    eff = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    ret5d = _sf(audit.get("return_5d_pct"))
    rel5 = _sf(audit.get("rel_return_5d_vs_nifty_pct"))
    cmf = _sf(audit.get("cmf_20"))
    extreme_move = bool(audit.get("extreme_price_move_proxy"))

    core = (
        bool(a.get("buy_allowed"))
        and not bool(audit.get("trap_exit_proxy"))
        and not bool(audit.get("liquidity_thin_proxy"))
        and not extreme_move
        and not math.isnan(next_week)
        and next_week >= 70.0
        and not math.isnan(eff)
        and eff >= 65.0
        and not (not math.isnan(ret5d) and ret5d <= -4.0)
        and not (not math.isnan(rel5) and rel5 <= -3.0)
    )
    flow_ok = math.isnan(cmf) or cmf >= -0.05
    not_overextended = not bool(c.get("over_extension_hot"))
    clean = core and flow_ok and not_overextended
    return {"clean_buy": clean, "constructive_core": core}


def _map_label(risk_net: float, gate: dict[str, bool], a: dict[str, Any]) -> str:
    """Score -> label (before forced overrides / hysteresis)."""
    if risk_net >= 7.0:
        return "exit-risk"
    if risk_net >= 4.0:
        return "trim"
    # risk_net < 4
    if gate["clean_buy"]:
        return "buy"
    if gate["constructive_core"]:
        # constructive-but-capped (flow / over-extension / confidence cap)
        return "accumulate"
    return "hold"


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


def _apply_hysteresis(
    label: str,
    risk_net: float,
    *,
    prior_label: str | None,
    bypass: bool,
    buffer: float,
) -> tuple[str, bool]:
    """Asymmetric hysteresis: constructive transitions are sticky, danger is fast.

    Returns (label, applied). When there is no prior-session label available this is a
    no-op (see module note: persistence-based stickiness is deferred until prior-session
    plumbing is wired). Danger transitions and Tier-1 bypass apply same-day.
    """
    if prior_label is None or prior_label == label:
        return label, False
    if bypass or label == "exit-risk":
        return label, False  # fast out on genuine danger

    # Buffer-based stickiness around the trim edge (4.0) for hold<->trim churn.
    if {prior_label, label} == {"hold", "trim"}:
        if label == "trim" and risk_net < 4.0 + buffer:
            return prior_label, True
        if label == "hold" and risk_net > 4.0 - buffer:
            return prior_label, True
        return label, False

    # Entering a constructive label (buy/accumulate) requires margin below the edge.
    if label in ("buy", "accumulate") and risk_net > 4.0 - buffer:
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
    dominant_bear = final_label in ("trim", "exit-risk") or risk_net >= 4.0
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

    agg = _aggregate(c, d)
    risk_c = agg["risk_c"]
    bull_c = agg["bull_c"]

    bull_offset = _env_float("TITAN_SIGV2_E_BULL_OFFSET", 0.5)
    buffer = _env_float("TITAN_SIGV2_E_HYST_BUFFER", 0.5)

    risk_net = _clamp(risk_c - bull_offset * bull_c, 0.0, 10.0)

    gate = _buy_gate(audit, a, c)
    label = _map_label(risk_net, gate, a)
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
    label = _escalate(label, b.get("forced_label"))

    bypass = bool(b.get("bypass_hysteresis"))
    prior_label = audit.get("prev_action_signal")
    prior_label = str(prior_label).strip().lower() if prior_label else None
    label, _hyst = _apply_hysteresis(
        label, risk_net, prior_label=prior_label, bypass=bypass, buffer=buffer
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
    }
    audit["signal_engine_version"] = "v2"

    return label, round(risk_net, 2), reasons[:8]
