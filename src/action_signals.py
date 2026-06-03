"""Unified action labels (BUY / HOLD / TRIM / EXIT) and UI/email color tokens."""

from __future__ import annotations

import math
import re
from typing import Any

# Gmail-safe inline styles (also used by insights HTML).
ACTION_STYLES: dict[str, dict[str, str]] = {
    "buy": {
        "fg": "#137333",
        "bg": "#e6f4ea",
        "border": "#34a853",
        "badge": "#34a853",
    },
    "accumulate": {
        "fg": "#0b6b5e",
        "bg": "#e0f7f3",
        "border": "#12a594",
        "badge": "#12a594",
    },
    "hold": {
        "fg": "#b06000",
        "bg": "#fef7e0",
        "border": "#fbbc05",
        "badge": "#fbbc05",
    },
    "trim": {
        "fg": "#c5221f",
        "bg": "#fce8e6",
        "border": "#ea4335",
        "badge": "#ea4335",
    },
    "exit-risk": {
        "fg": "#c5221f",
        "bg": "#fce8e6",
        "border": "#ea4335",
        "badge": "#ea4335",
    },
}


def normalize_action_signal(signal: str | None) -> str:
    s = str(signal or "hold").strip().lower().replace("_", "-")
    if s in ("exit", "exit-risk", "sell", "exitrisk"):
        return "exit-risk"
    if s in ("buy", "add", "buy-more", "buymore"):
        return "buy"
    if s in ("accumulate", "acc", "accumulate-dip"):
        return "accumulate"
    if s == "trim":
        return "trim"
    return "hold"


def action_signal_plain_english(signal: str) -> str:
    """Human-readable headline fragment (compliance-safe product label, not LLM advice)."""
    key = normalize_action_signal(signal)
    return {
        "buy": (
            "BUY — constructive setup (next-week & intent supportive; "
            "add exposure per your mandate)"
        ),
        "accumulate": (
            "ACCUMULATE — constructive but extended; add on pullbacks rather than chase"
        ),
        "hold": "HOLD — risk score <4: no strong defensive trigger",
        "trim": "TRIM — risk score 4–6: lighten / take profits (below hard-exit bar)",
        "exit-risk": (
            "EXIT RISK — risk score ≥7: hard exit bar — cut exposure sharply or exit"
        ),
    }[key]


def action_style(signal: str) -> dict[str, str]:
    return dict(ACTION_STYLES[normalize_action_signal(signal)])


def action_signal_from_digest_headline(line: str) -> str | None:
    """Parse BUY/HOLD/TRIM/EXIT from a sector digest symbol headline."""
    upper = line.upper()
    if "EXIT RISK" in upper or "EXIT-RISK" in upper:
        return "exit-risk"
    # Order matters: EXIT before TRIM substring checks.
    if re.search(r"\bBUY\b", upper):
        return "buy"
    if re.search(r"\bACCUMULATE\b", upper):
        return "accumulate"
    if re.search(r"\bTRIM\b", upper):
        return "trim"
    if re.search(r"\bHOLD\b", upper):
        return "hold"
    return None


def _safe_float(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _fmt_metric(v: Any) -> str:
    x = _safe_float(v)
    if math.isnan(x):
        return "n/a"
    return f"{x:.2f}"


def _append_capped(
    acc: float,
    cap: float,
    points: float,
    reason: str,
    reasons: list[str],
) -> float:
    """Add ``points`` toward ``cap``; record ``reason`` if any material points land."""
    if points <= 0 or cap <= 0:
        return acc
    room = max(0.0, cap - acc)
    take = min(points, room)
    if take > 0.05:
        reasons.append(reason)
    return acc + take


def derive_action_signal(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    """Dispatch to the v2 layered engine when ``TITAN_SIGNAL_V2`` is set, else legacy.

    With the master flag off this is byte-identical to the legacy path below.
    """
    from signal_v2 import evaluate_signal_v2, v2_enabled

    if v2_enabled():
        return evaluate_signal_v2(audit)
    return _derive_action_signal_legacy(audit)


def _derive_action_signal_legacy(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    """
    Defensive risk score (capped per family to limit double-counting) plus BUY when
    tape supports adding exposure.

    Returns (action_signal, risk_score, reasons).
    Stored on audits as ``sell_signal`` for backward compatibility.
    """
    reasons: list[str] = []

    next_week = _safe_float(audit.get("next_week_score"))
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ret1d = _safe_float(audit.get("return_1d_pct"))
    ret5d = _safe_float(audit.get("return_5d_pct"))
    ret10d = _safe_float(audit.get("return_10d_pct"))
    rel5 = _safe_float(audit.get("rel_return_5d_vs_nifty_pct"))
    ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
    atr_pct = _safe_float(audit.get("atr_14_pct"))
    atr_pi = _safe_float(audit.get("atr_penalty_input"))
    extreme_move = bool(audit.get("extreme_price_move_proxy"))
    ret1d_weight = 0.45 if extreme_move else 1.0

    risk = 0.0

    # Horizon (next-week tape) — cap 3
    h = 0.0
    hr: list[str] = []
    if not math.isnan(next_week):
        if next_week < 45.0:
            h = _append_capped(h, 3.0, 3.0, f"nextWeek weak {_fmt_metric(next_week)}", hr)
        elif next_week < 55.0:
            h = _append_capped(h, 3.0, 1.0, f"nextWeek soft {_fmt_metric(next_week)}", hr)
    risk += min(3.0, h)
    reasons.extend(hr[:2])

    # Intent — cap 2
    inc = 0.0
    ir: list[str] = []
    if not math.isnan(eff):
        if eff < 45.0:
            inc = _append_capped(inc, 2.0, 2.0, f"intent defensive {_fmt_metric(eff)}", ir)
        elif eff < 52.0:
            inc = _append_capped(inc, 2.0, 1.0, f"intent cooling {_fmt_metric(eff)}", ir)
    risk += min(2.0, inc)
    reasons.extend(ir[:2])

    # Z — cap 2
    zc = 0.0
    zr: list[str] = []
    if not math.isnan(z):
        if z <= -2.0:
            zc = _append_capped(zc, 2.0, 2.0, f"z bearish {_fmt_metric(z)}", zr)
        elif z <= -1.0:
            zc = _append_capped(zc, 2.0, 1.0, f"z below mean {_fmt_metric(z)}", zr)
    risk += min(2.0, zc)
    reasons.extend(zr[:1])

    # Multi-day momentum — cap 3 (1d down-weighted when extreme-move proxy fires)
    mom = 0.0
    mr: list[str] = []
    if not math.isnan(ret1d):
        if ret1d <= -2.0:
            mom = _append_capped(
                mom,
                3.0,
                2.0 * ret1d_weight,
                f"1d return weak {_fmt_metric(ret1d)}%",
                mr,
            )
        elif ret1d <= -1.0:
            mom = _append_capped(
                mom,
                3.0,
                1.0 * ret1d_weight,
                f"1d return soft {_fmt_metric(ret1d)}%",
                mr,
            )
    if not math.isnan(ret5d):
        if ret5d <= -6.0:
            mom = _append_capped(mom, 3.0, 2.0, f"5d trend weak {_fmt_metric(ret5d)}%", mr)
        elif ret5d <= -3.5:
            mom = _append_capped(mom, 3.0, 1.5, f"5d drift soft {_fmt_metric(ret5d)}%", mr)
        elif ret5d <= -2.0:
            mom = _append_capped(mom, 3.0, 1.0, f"5d soft {_fmt_metric(ret5d)}%", mr)
    if not math.isnan(ret10d):
        if ret10d <= -10.0:
            mom = _append_capped(mom, 3.0, 1.5, f"10d base weak {_fmt_metric(ret10d)}%", mr)
        elif ret10d <= -6.0:
            mom = _append_capped(mom, 3.0, 1.0, f"10d soft {_fmt_metric(ret10d)}%", mr)
    risk += min(3.0, mom)
    reasons.extend(mr[:2])

    # Trend vs 200d — cap 2
    em = 0.0
    er: list[str] = []
    if not math.isnan(ema_dist):
        if ema_dist <= -6.0:
            em = _append_capped(em, 2.0, 2.0, f"below ema200 {_fmt_metric(ema_dist)}%", er)
        elif ema_dist <= -2.0:
            em = _append_capped(em, 2.0, 1.0, f"ema200 drift {_fmt_metric(ema_dist)}%", er)
    risk += min(2.0, em)
    reasons.extend(er[:1])

    # Volatility — cap 2 (prefer ATR vs sector median when available)
    vo = 0.0
    vr: list[str] = []
    if not math.isnan(atr_pi):
        if atr_pi >= 2.2:
            vo = _append_capped(vo, 2.0, 2.0, f"ATR vs sector hot {_fmt_metric(atr_pi)}×", vr)
        elif atr_pi >= 1.6:
            vo = _append_capped(vo, 2.0, 1.5, f"ATR vs sector elevated {_fmt_metric(atr_pi)}×", vr)
        elif atr_pi >= 1.25:
            vo = _append_capped(vo, 2.0, 1.0, f"ATR vs sector high {_fmt_metric(atr_pi)}×", vr)
    elif not math.isnan(atr_pct):
        if atr_pct >= 6.0:
            vo = _append_capped(vo, 2.0, 2.0, f"atr elevated {_fmt_metric(atr_pct)}%", vr)
        elif atr_pct >= 4.0:
            vo = _append_capped(vo, 2.0, 1.0, f"atr high {_fmt_metric(atr_pct)}%", vr)
    risk += min(2.0, vo)
    reasons.extend(vr[:1])

    # Overlays / guardrails / tape stress — cap 4 (decorrelate correlated alarms)
    ov = 0.0
    or_: list[str] = []
    if audit.get("trap_exit_proxy"):
        ov = _append_capped(ov, 4.0, 2.0, "trap-exit proxy", or_)
    if audit.get("high_volume_down_day_proxy") or audit.get("panic_absorption_proxy"):
        ov = _append_capped(ov, 4.0, 1.0, "high-volume down-day stress", or_)
    if audit.get("cluster_guardrail_applied"):
        ov = _append_capped(ov, 4.0, 1.0, "cluster guardrail", or_)
    if audit.get("macro_guardrail_applied"):
        ov = _append_capped(ov, 4.0, 1.0, "macro guardrail", or_)
    if audit.get("event_guardrail_applied") or audit.get("event_risk_soon"):
        ov = _append_capped(ov, 4.0, 1.0, "event risk", or_)
    if audit.get("liquidity_thin_proxy"):
        ov = _append_capped(ov, 4.0, 1.0, "liquidity thin vs peers/floor", or_)
    risk += min(4.0, ov)
    reasons.extend(or_[:2])

    f_status = str(audit.get("fundamental_status") or "unavailable")
    if f_status == "weak":
        risk += 2.0
        reasons.append("fundamentals weak")
    elif f_status == "balanced":
        risk += 1.0
        reasons.append("fundamentals balanced")
    elif f_status == "strong":
        risk = max(0.0, risk - 1.0)

    risk = round(min(10.0, risk), 2)

    buy_reasons: list[str] = []
    thin = bool(audit.get("liquidity_thin_proxy"))
    can_buy = (
        risk < 4.0
        and not audit.get("trap_exit_proxy")
        and not thin
        and not extreme_move
        and not math.isnan(next_week)
        and next_week >= 70.0
        and not math.isnan(eff)
        and eff >= 65.0
    )
    if can_buy and not math.isnan(ret5d) and ret5d <= -4.0:
        can_buy = False
    if can_buy and not math.isnan(rel5) and rel5 <= -3.0:
        can_buy = False

    if can_buy:
        buy_reasons.append(f"nextWeek strong {_fmt_metric(next_week)}")
        buy_reasons.append(f"intent supportive {_fmt_metric(eff)}")
        if not math.isnan(ret1d) and ret1d > 0:
            buy_reasons.append(f"1d momentum {_fmt_metric(ret1d)}%")
        if f_status == "strong":
            buy_reasons.append("fundamentals strong")
        return "buy", risk, buy_reasons[:4]

    if risk >= 7.0:
        signal = "exit-risk"
    elif risk >= 4.0:
        signal = "trim"
    else:
        signal = "hold"

    if not reasons:
        reasons = ["technical+risk profile stable"]
    return signal, risk, reasons[:4]
