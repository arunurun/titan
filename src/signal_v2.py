"""Flag-gated v2 signal engine: a layered (A-E) waterfall over the same audit dict.

Default-off. The whole engine is engaged only when ``TITAN_SIGNAL_V2`` is truthy;
when it is off, ``action_signals.derive_action_signal`` runs the legacy code path and
output is byte-identical to today. Each layer has its own sub-flag (default *on* once
the master flag is engaged) so layers can be ablated individually for A/B testing.

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
    """Master switch for the v2 engine."""
    return _env_truthy("TITAN_SIGNAL_V2", default=False)


def _layer_enabled(flag: str) -> bool:
    """Per-layer ablation flag; defaults *on* once the master flag is engaged."""
    return _env_truthy(flag, default=True)


def _accumulate_enabled() -> bool:
    return _env_truthy("TITAN_SIGV2_ENABLE_ACCUMULATE", default=False)


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
    if not _layer_enabled("TITAN_SIGNAL_V2_LAYER_A"):
        return out

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
    if not _layer_enabled("TITAN_SIGNAL_V2_LAYER_C"):
        return {
            "families": {},
            "money_flow_bear": 0.0,
            "money_flow_bull": 0.0,
            "over_extension": 0.0,
            "over_extension_hot": False,
            "fundamental": 0.0,
            "bull_terms": 0.0,
            "trace": [],
        }

    fp = _family_points(audit)
    trace: list[dict[str, Any]] = list(fp["trace"])

    cmf = _sf(audit.get("cmf_20"))
    obv = _sf(audit.get("obv_slope_20"))
    stretch = _sf(audit.get("ema200_stretch_atr"))
    stretch_pctile = _sf(audit.get("sector_pctile_ema200_stretch"))

    k_cmf = _env_float("TITAN_SIGV2_C_CMF_K", 10.0)
    cap_cmf = _env_float("TITAN_SIGV2_C_CMF_CAP", 2.0)
    stretch_deadband = _env_float("TITAN_SIGV2_C_STRETCH_DEADBAND_ATR", 4.0)
    stretch_ramp = _env_float("TITAN_SIGV2_C_STRETCH_RAMP_ATR", 8.0)
    stretch_cap = _env_float("TITAN_SIGV2_C_STRETCH_CAP", 2.0)

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
    if not _layer_enabled("TITAN_SIGNAL_V2_LAYER_D"):
        return out

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
    if not _layer_enabled("TITAN_SIGNAL_V2_LAYER_B"):
        return out

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
    """Layer-A ceiling caps the *constructive* side only (never blocks downgrades)."""
    if ceiling == "hold" and label in ("buy", "accumulate"):
        return "hold"
    return label


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
    label = _apply_ceiling(label, a.get("label_ceiling"))
    label = _escalate(label, b.get("forced_label"))

    # Accumulate emission is gated; when disabled, collapse to its legacy neighbor.
    if label == "accumulate" and not _accumulate_enabled():
        label = "hold"

    bypass = bool(b.get("bypass_hysteresis"))
    prior_label = audit.get("prev_action_signal")
    prior_label = str(prior_label).strip().lower() if prior_label else None
    if _layer_enabled("TITAN_SIGNAL_V2_LAYER_E"):
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
