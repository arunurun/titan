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


def digest_headline_text(audit: dict[str, Any], action: dict[str, Any] | None = None) -> str:
    """Single-line digest headline with canonical REDUCE/EXIT/BUY labels."""
    act = action or derive_full_action(audit)
    label = str(act.get("label") or "hold").upper()
    pos = act.get("position_size_pct")
    exp_ret = act.get("expected_return_5d_pct")
    parts = [label]
    if pos is not None:
        parts.append(f"size {pos:.0f}%")
    if exp_ret is not None:
        parts.append(f"5D {exp_ret:+.1f}%")
    return " — ".join(parts)


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
