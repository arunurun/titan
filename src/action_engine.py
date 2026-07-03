"""Full action recommendation — display labels, sizing, and horizon expectations."""

from __future__ import annotations

import math
from typing import Any

_DISPLAY_LABEL_MAP: dict[str, str] = {
    "buy": "buy",
    "accumulate": "accumulate",
    "hold": "hold",
    "trim": "reduce",
    "exit-risk": "exit",
    "exit": "exit",
    "reduce": "reduce",
}


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _display_label(internal: str) -> str:
    key = str(internal or "hold").strip().lower().replace("_", "-")
    return _DISPLAY_LABEL_MAP.get(key, "hold")


def _expected_return_5d_pct(audit: dict[str, Any]) -> float | None:
    """Map calibrated probability or next-week score to an indicative 5d return %."""
    prob = audit.get("predicted_probability")
    if prob is None:
        cal = audit.get("probability_calibration")
        if isinstance(cal, dict):
            prob = cal.get("predicted_probability")
    p = _sf(prob) if prob is not None else float("nan")
    if not math.isnan(p):
        return round(_clamp((p - 0.5) * 12.0, -8.0, 8.0), 2)

    nw = _sf(audit.get("next_week_score"))
    if not math.isnan(nw):
        return round(_clamp((nw - 50.0) * 0.12, -8.0, 8.0), 2)
    return None


def _expected_drawdown_5d_pct(audit: dict[str, Any]) -> float | None:
    """ATR + stretch proxies for indicative 5d drawdown risk."""
    parts: list[float] = []
    atr = _sf(audit.get("atr_14_pct"))
    if not math.isnan(atr) and atr > 0:
        parts.append(atr * 1.25)
    stretch = _sf(audit.get("stretch_composite"))
    if math.isnan(stretch):
        stretch = _sf(audit.get("ema_200_distance_pct"))
    if not math.isnan(stretch) and stretch > 0:
        parts.append(min(12.0, stretch * 0.18))
    risk = _sf(audit.get("sell_signal_risk_score"))
    if not math.isnan(risk) and risk >= 4.0:
        parts.append(min(10.0, risk * 0.9))
    if not parts:
        return None
    return round(_clamp(sum(parts) / len(parts), 0.5, 15.0), 2)


def _position_size_pct(audit: dict[str, Any]) -> float | None:
    pos = audit.get("position_confidence")
    if pos is None:
        pos = audit.get("position_score")
    if pos is not None:
        p = _sf(pos)
        if not math.isnan(p):
            return round(_clamp(p * 100.0, 0.0, 100.0), 1)
    try:
        from probability_calibration import compute_position_score

        tech = audit.get("technical_confidence")
        if tech is None:
            tech = audit.get("signal_confidence")
        sized = compute_position_score(
            audit.get("predicted_probability"),
            tech,
        )
        if sized is not None:
            return round(_clamp(float(sized) * 100.0, 0.0, 100.0), 1)
    except ImportError:
        pass
    nw = _sf(audit.get("next_week_score"))
    if not math.isnan(nw):
        return round(_clamp((nw / 100.0) * 85.0, 5.0, 100.0), 1)
    return None


def _thresholds_used(audit: dict[str, Any], risk_net: float) -> dict[str, Any]:
    trace = audit.get("signal_reason_trace")
    if not isinstance(trace, dict):
        trace = {}
    return {
        "risk_net": round(risk_net, 3) if not math.isnan(_sf(risk_net)) else None,
        "trim_threshold": 4.0,
        "exit_threshold": 7.0,
        "buy_next_week_gate": 65.0,
        "buy_intent_gate": 60.0,
        "forced_label": audit.get("forced_label") or trace.get("forced_label"),
    }


_DISPLAY_HEADLINE: dict[str, str] = {
    "buy": (
        "BUY — constructive setup (next-week & intent supportive; "
        "add exposure per your mandate)"
    ),
    "accumulate": (
        "ACCUMULATE — constructive but extended; add on pullbacks rather than chase"
    ),
    "hold": "HOLD — risk score <4: no strong defensive trigger",
    "reduce": "REDUCE — risk score 4–6: lighten / take profits (below hard-exit bar)",
    "exit": "EXIT — risk score ≥7: hard exit bar — cut exposure sharply or exit",
}


def digest_headline_text(audit: dict[str, Any], action: dict[str, Any] | None = None) -> str:
    """Digest/email symbol headline using canonical display labels (reduce/exit not trim)."""
    payload = action if isinstance(action, dict) else derive_full_action(audit)
    label = str(payload.get("label") or "hold").strip().lower()
    return _DISPLAY_HEADLINE.get(label, _DISPLAY_HEADLINE["hold"])


def conviction_band(score: float) -> str:
    """Map 0–100 conviction score to low / moderate / high."""
    if math.isnan(score):
        return "n/a"
    if score >= 70.0:
        return "high"
    if score >= 40.0:
        return "moderate"
    return "low"


def _short_term_tilt_descriptor(ret_pct: float) -> str:
    if math.isnan(ret_pct):
        return "unclear"
    if ret_pct >= 3.0:
        return "strongly positive"
    if ret_pct >= 1.0:
        return "positive"
    if ret_pct >= 0.3:
        return "slightly positive"
    if ret_pct > -0.3:
        return "neutral"
    if ret_pct > -1.0:
        return "slightly negative"
    if ret_pct > -3.0:
        return "negative"
    return "strongly negative"


def _win_odds_pct(audit: dict[str, Any]) -> float | None:
    prob = audit.get("predicted_probability")
    if prob is None:
        cal = audit.get("probability_calibration")
        if isinstance(cal, dict):
            prob = cal.get("predicted_probability")
    p = _sf(prob) if prob is not None else float("nan")
    if math.isnan(p):
        return None
    return round(p * 100.0, 1)


def format_conviction_digest_line(action: dict[str, Any]) -> str | None:
    pos = action.get("position_size_pct")
    if pos is None:
        return None
    band = conviction_band(float(pos))
    return (
        f"Conviction score: {pos:.0f}/100 ({band}) — "
        "model blend of 5-day win odds + technical strength; "
        "not a portfolio allocation"
    )


def format_short_term_tilt_digest_lines(
    action: dict[str, Any], audit: dict[str, Any]
) -> list[str]:
    exp_ret = action.get("expected_return_5d_pct")
    exp_dd = action.get("expected_drawdown_5d_pct")
    if exp_ret is None and exp_dd is None:
        return []
    ret_f = _sf(exp_ret) if exp_ret is not None else float("nan")
    dd_f = _sf(exp_dd) if exp_dd is not None else float("nan")
    tilt = _short_term_tilt_descriptor(ret_f)
    if not math.isnan(ret_f):
        ret_part = f"{tilt} ({ret_f:+.1f}% indicative)"
    else:
        ret_part = tilt
    if not math.isnan(dd_f):
        vol_part = f"typical volatility band ~{dd_f:.1f}% (from ATR)"
    else:
        vol_part = "volatility band n/a"
    lines = [f"Short-term tilt: {ret_part} · {vol_part}"]
    inputs: list[str] = []
    win_odds = _win_odds_pct(audit)
    if win_odds is not None:
        inputs.append(f"win odds {win_odds:.0f}%")
    atr = _sf(audit.get("atr_14_pct"))
    if not math.isnan(atr) and atr > 0:
        inputs.append(f"ATR {atr:.1f}%")
    if inputs:
        lines.append(f"based on {' and '.join(inputs)}")
    return lines


def format_buy_checklist_digest_line(audit: dict[str, Any]) -> str | None:
    """Plain-English buy gate for HOLD names (next-week + intent thresholds)."""
    sell_signal = str(audit.get("sell_signal", "unknown")).strip().lower()
    risk_net = _sf(audit.get("sell_signal_risk_score"))
    if sell_signal != "hold":
        return None
    if not math.isnan(risk_net) and risk_net >= 4.0:
        return None

    nw_gate = 65.0
    intent_gate = 60.0
    nw_val = _sf(audit.get("next_week_score"))
    intent_val = _sf(
        audit.get("effective_intent_score", audit.get("intent_score"))
    )

    nw_pass = not math.isnan(nw_val) and nw_val >= nw_gate
    intent_pass = not math.isnan(intent_val) and intent_val >= intent_gate
    nw_disp = f"{nw_val:.0f}" if not math.isnan(nw_val) else "n/a"
    intent_disp = f"{intent_val:.0f}" if not math.isnan(intent_val) else "n/a"
    nw_mark = "✓ passes" if nw_pass else "✗ fails"
    intent_mark = "✓ passes" if intent_pass else "✗ fails"
    verdict = "BUY eligible" if (nw_pass and intent_pass) else "HOLD not BUY"

    return (
        f"Buy checklist: 1-week outlook {nw_disp} {nw_mark} (need ≥{nw_gate:.0f}) · "
        f"technical intent {intent_disp} {intent_mark} (need ≥{intent_gate:.0f}) — "
        f"{verdict}"
    )


def action_recommendation_digest_lines(audit: dict[str, Any]) -> list[str]:
    """Indented digest detail lines under the symbol headline (conviction + short-term tilt)."""
    action = derive_full_action(audit)
    audit["full_action"] = action
    lines: list[str] = []
    conv = format_conviction_digest_line(action)
    if conv:
        lines.append(f"   {conv}")
    for tilt_line in format_short_term_tilt_digest_lines(action, audit):
        indent = "   " if tilt_line.startswith("based on") else "   "
        lines.append(f"{indent}{tilt_line}")
    return lines


def derive_full_action(audit: dict[str, Any]) -> dict[str, Any]:
    """
    Canonical action payload for digest/email and downstream consumers.

    Returns:
        label: buy|accumulate|hold|reduce|exit (display canonical)
        label_internal: trim|exit-risk|... (legacy value preserved)
        confidence, expected_return_5d_pct, expected_drawdown_5d_pct,
        position_size_pct, reasons, thresholds_used
    """
    from action_signals import derive_action_signal

    label_internal, risk_net, reasons = derive_action_signal(audit)
    display = _display_label(label_internal)

    conf_raw = audit.get("signal_confidence")
    if conf_raw is None:
        trace = audit.get("signal_reason_trace")
        if isinstance(trace, dict):
            conf_raw = trace.get("confidence")
    conf = _sf(conf_raw) if conf_raw is not None else float("nan")
    confidence = round(_clamp(conf, 0.0, 1.0), 3) if not math.isnan(conf) else None

    return {
        "label": display,
        "label_internal": label_internal,
        "confidence": confidence,
        "expected_return_5d_pct": _expected_return_5d_pct(audit),
        "expected_drawdown_5d_pct": _expected_drawdown_5d_pct(audit),
        "position_size_pct": _position_size_pct(audit),
        "reasons": list(reasons or [])[:8],
        "thresholds_used": _thresholds_used(audit, risk_net),
    }
