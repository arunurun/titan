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


def derive_action_signal(audit: dict[str, Any]) -> tuple[str, float, list[str]]:
    """
  Risk score (defensive) plus constructive BUY when tape supports adding exposure.

  Returns (action_signal, risk_score, reasons).
  Stored on audits as ``sell_signal`` for backward compatibility.
    """
    risk = 0.0
    reasons: list[str] = []

    def add(points: float, reason: str) -> None:
        nonlocal risk
        risk += points
        reasons.append(reason)

    next_week = _safe_float(audit.get("next_week_score"))
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ret1d = _safe_float(audit.get("return_1d_pct"))
    ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
    atr_pct = _safe_float(audit.get("atr_14_pct"))

    if not math.isnan(next_week):
        if next_week < 45.0:
            add(3.0, f"nextWeek weak {_fmt_metric(next_week)}")
        elif next_week < 55.0:
            add(1.0, f"nextWeek soft {_fmt_metric(next_week)}")
    if not math.isnan(eff):
        if eff < 45.0:
            add(2.0, f"intent defensive {_fmt_metric(eff)}")
        elif eff < 52.0:
            add(1.0, f"intent cooling {_fmt_metric(eff)}")
    if not math.isnan(z):
        if z <= -2.0:
            add(2.0, f"z bearish {_fmt_metric(z)}")
        elif z <= -1.0:
            add(1.0, f"z below mean {_fmt_metric(z)}")
    if not math.isnan(ret1d):
        if ret1d <= -2.0:
            add(2.0, f"1d return weak {_fmt_metric(ret1d)}%")
        elif ret1d <= -1.0:
            add(1.0, f"1d return soft {_fmt_metric(ret1d)}%")
    if not math.isnan(ema_dist):
        if ema_dist <= -6.0:
            add(2.0, f"below ema200 {_fmt_metric(ema_dist)}%")
        elif ema_dist <= -2.0:
            add(1.0, f"ema200 drift {_fmt_metric(ema_dist)}%")
    if not math.isnan(atr_pct):
        if atr_pct >= 6.0:
            add(2.0, f"atr elevated {_fmt_metric(atr_pct)}%")
        elif atr_pct >= 4.0:
            add(1.0, f"atr high {_fmt_metric(atr_pct)}%")

    if audit.get("trap_exit_proxy"):
        add(2.0, "trap-exit proxy")
    if audit.get("panic_absorption_proxy"):
        add(1.0, "panic volume-participation proxy")
    if audit.get("cluster_guardrail_applied"):
        add(1.0, "cluster guardrail")
    if audit.get("macro_guardrail_applied"):
        add(1.0, "macro guardrail")
    if audit.get("event_guardrail_applied") or audit.get("event_risk_soon"):
        add(1.0, "event risk")

    f_status = str(audit.get("fundamental_status") or "unavailable")
    if f_status == "weak":
        add(2.0, "fundamentals weak")
    elif f_status == "balanced":
        add(1.0, "fundamentals balanced")
    elif f_status == "strong":
        risk = max(0.0, risk - 1.0)

    risk = round(risk, 2)

    buy_reasons: list[str] = []
    if (
        risk < 4.0
        and not audit.get("trap_exit_proxy")
        and not math.isnan(next_week)
        and next_week >= 70.0
        and not math.isnan(eff)
        and eff >= 65.0
    ):
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
