"""Structured investment report from a completed Titan audit dict."""

from __future__ import annotations

import math
from typing import Any

from action_signals import action_signal_plain_english, normalize_action_signal


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _fmt(v: Any, digits: int = 1) -> str:
    x = _sf(v)
    if math.isnan(x):
        return "n/a"
    return f"{x:.{digits}f}"


def _factor_block(audit: dict[str, Any], key: str) -> dict[str, Any]:
    scores = audit.get("factor_scores")
    if isinstance(scores, dict) and isinstance(scores.get(key), dict):
        return scores[key]
    return {}


def _lines_from_factor(block: dict[str, Any], *, fallback: str) -> list[str]:
    if not block or not block.get("available"):
        return [fallback]
    score = block.get("score")
    reasons = block.get("reasons") if isinstance(block.get("reasons"), list) else []
    conf = block.get("confidence")
    head = f"Score {_fmt(score)}/100"
    if conf is not None:
        head += f" (confidence {_fmt(conf, 2)})"
    lines = [head]
    for reason in reasons[:4]:
        lines.append(f"- {reason}")
    return lines


def _technical_summary(audit: dict[str, Any]) -> list[str]:
    fusion = audit.get("titan_fusion") if isinstance(audit.get("titan_fusion"), dict) else {}
    lines = _lines_from_factor(_factor_block(audit, "technical"), fallback="Technical inputs unavailable.")
    intent = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    if not math.isnan(intent):
        lines.append(f"Effective intent: {_fmt(intent)}/100")
    z = _sf(audit.get("z_score"))
    if not math.isnan(z):
        lines.append(f"1D z-score: {_fmt(z, 2)}")
    if fusion.get("technical_score") is not None:
        lines.append(f"Fusion technical pillar: {_fmt(fusion.get('technical_score'))}")
    return lines


def _fundamental_summary(audit: dict[str, Any]) -> list[str]:
    block = _factor_block(audit, "fundamentals")
    if block.get("available"):
        return _lines_from_factor(block, fallback="Fundamentals unavailable.")
    status = str(audit.get("fundamental_status") or "unavailable")
    score = audit.get("fundamental_score")
    reasons = audit.get("fundamental_reasons") if isinstance(audit.get("fundamental_reasons"), list) else []
    lines = [f"Status: {status} · score {_fmt(score)}"]
    for reason in reasons[:3]:
        lines.append(f"- {reason}")
    return lines


def _flow_summary(audit: dict[str, Any]) -> list[str]:
    flow = audit.get("institutional_flow")
    if isinstance(flow, dict) and flow.get("available"):
        lines = [f"Flow score {_fmt(flow.get('score'))}/100"]
        for reason in (flow.get("reasons") or [])[:4]:
            lines.append(f"- {reason}")
        return lines
    return _lines_from_factor(_factor_block(audit, "institutional_flow"), fallback="Flow data unavailable.")


def _sector_summary(audit: dict[str, Any]) -> list[str]:
    sector = str(audit.get("sector") or audit.get("sector_key") or "unknown")
    lines = [f"Sector: {sector}"]
    rotation = audit.get("sector_rotation")
    if isinstance(rotation, dict) and rotation.get("available"):
        lines.append(f"Rotation score {_fmt(rotation.get('score'))}/100")
        for reason in (rotation.get("reasons") or [])[:3]:
            lines.append(f"- {reason}")
    else:
        block = _factor_block(audit, "sector_strength")
        lines.extend(_lines_from_factor(block, fallback="Sector strength unavailable."))
    pctile = _sf(audit.get("sector_relative_strength_pctile"))
    if not math.isnan(pctile):
        lines.append(f"Sector RS percentile: {_fmt(pctile)}")
    return lines


def _market_summary(audit: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    regime = audit.get("market_regime")
    if isinstance(regime, dict):
        label = regime.get("regime") or regime.get("raw_regime") or "unknown"
        streak = regime.get("streak")
        lines.append(f"Regime: {label}" + (f" (streak {streak})" if streak is not None else ""))
    fusion = audit.get("titan_fusion") if isinstance(audit.get("titan_fusion"), dict) else {}
    if fusion.get("regime_score") is not None:
        lines.append(f"Fusion regime pillar: {_fmt(fusion.get('regime_score'))}")

    breadth = audit.get("market_breadth")
    if isinstance(breadth, dict) and breadth.get("n_symbols"):
        lines.append(
            f"Breadth panel: {breadth.get('n_symbols')} symbols · "
            f"% above EMA200 {_fmt(breadth.get('pct_above_ema200'))}"
        )
    elif audit.get("breadth_above_ema200_pct") is not None:
        lines.append(f"Breadth % above EMA200: {_fmt(audit.get('breadth_above_ema200_pct'))}")
    if fusion.get("breadth_score") is not None:
        lines.append(f"Breadth diagnostic score: {_fmt(fusion.get('breadth_score'))} (not weighted in titan_score)")
    if not lines:
        lines.append("Market context unavailable.")
    return lines


def _risk_summary(audit: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    risk_block = _factor_block(audit, "risk")
    if risk_block.get("available"):
        lines.extend(_lines_from_factor(risk_block, fallback=""))
    risk_net = _sf(audit.get("sell_signal_risk_score"))
    if not math.isnan(risk_net):
        lines.append(f"Signal risk net: {_fmt(risk_net, 2)}/10")
    atr = _sf(audit.get("atr_14_pct"))
    if not math.isnan(atr):
        lines.append(f"ATR14: {_fmt(atr)}%")
    reasons = audit.get("sell_signal_reasons") if isinstance(audit.get("sell_signal_reasons"), list) else []
    for reason in reasons[:3]:
        lines.append(f"- {reason}")
    if not lines:
        lines.append("Risk overlay unavailable.")
    return lines


def _outlook_fields(audit: dict[str, Any]) -> dict[str, Any]:
    next_week = _sf(audit.get("next_week_score"))
    next_day = _sf(audit.get("next_day_score"))
    atr = _sf(audit.get("atr_14_pct"))

    confidence = audit.get("fusion_confidence")
    if confidence is None:
        confidence = audit.get("signal_confidence")
    if confidence is None:
        prob = audit.get("probability_calibration")
        if isinstance(prob, dict):
            confidence = prob.get("position_confidence") or prob.get("technical_confidence")

    outlook = "neutral"
    if not math.isnan(next_week):
        if next_week >= 70:
            outlook = "constructive"
        elif next_week >= 55:
            outlook = "cautiously positive"
        elif next_week < 40:
            outlook = "defensive"

    expected_upside: float | None = None
    expected_downside: float | None = None
    reward_risk: float | None = None
    if not math.isnan(next_week):
        expected_upside = round(max(0.0, (next_week - 50.0) * 0.12), 2)
    if not math.isnan(atr):
        expected_downside = round(max(0.5, atr * 1.25), 2)
    if expected_upside is not None and expected_downside and expected_downside > 0:
        reward_risk = round(expected_upside / expected_downside, 2)

    return {
        "confidence": None if confidence is None else round(float(confidence), 3),
        "next_week_outlook": outlook,
        "next_day_score": None if math.isnan(next_day) else round(next_day, 1),
        "next_week_score": None if math.isnan(next_week) else round(next_week, 1),
        "expected_upside_pct": expected_upside,
        "expected_downside_pct": expected_downside,
        "reward_risk_ratio": reward_risk,
    }


def _section_md(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "n/a"
    return f"## {title}\n{body}"


def generate_investment_report(audit: dict[str, Any]) -> dict[str, Any]:
    """
    Build structured investment report from a scored audit.

    Reads action_signal / factor fields only — does not recompute labels or fusion.
    """
    symbol = str(audit.get("symbol") or "UNKNOWN")
    signal = normalize_action_signal(audit.get("action_signal") or audit.get("sell_signal") or "hold")
    fusion = audit.get("titan_fusion") if isinstance(audit.get("titan_fusion"), dict) else {}

    sections = {
        "technical_summary": _technical_summary(audit),
        "fundamental_summary": _fundamental_summary(audit),
        "flow_summary": _flow_summary(audit),
        "sector_summary": _sector_summary(audit),
        "market_summary": _market_summary(audit),
        "risk_summary": _risk_summary(audit),
        "final_decision": [
            f"Action: {signal.upper()} — {action_signal_plain_english(signal)}",
        ],
    }
    outlook = _outlook_fields(audit)
    sections["confidence"] = [f"Confidence: {_fmt(outlook.get('confidence'), 3)}"]
    sections["next_week_outlook"] = [f"Outlook: {outlook['next_week_outlook']}"]
    sections["expected_upside_pct"] = [f"Expected upside (heuristic): {_fmt(outlook.get('expected_upside_pct'))}%"]
    sections["expected_downside_pct"] = [f"Expected downside (ATR-based): {_fmt(outlook.get('expected_downside_pct'))}%"]
    sections["reward_risk_ratio"] = [f"Reward/risk: {_fmt(outlook.get('reward_risk_ratio'), 2)}"]

    md_parts = [
        f"# Titan Investment Report — {symbol}",
        "",
        _section_md("Technical Summary", sections["technical_summary"]),
        "",
        _section_md("Fundamental Summary", sections["fundamental_summary"]),
        "",
        _section_md("Flow Summary", sections["flow_summary"]),
        "",
        _section_md("Sector Summary", sections["sector_summary"]),
        "",
        _section_md("Market Summary", sections["market_summary"]),
        "",
        _section_md("Risk Summary", sections["risk_summary"]),
        "",
        _section_md("Final Decision", sections["final_decision"]),
        "",
        "## Outlook",
        f"Confidence: {_fmt(outlook.get('confidence'), 3)}",
        f"Next week: {outlook['next_week_outlook']} (score {_fmt(outlook.get('next_week_score'))})",
        f"Expected upside: {_fmt(outlook.get('expected_upside_pct'))}% · "
        f"downside: {_fmt(outlook.get('expected_downside_pct'))}% · "
        f"reward/risk: {_fmt(outlook.get('reward_risk_ratio'), 2)}",
    ]
    if fusion.get("titan_score") is not None:
        md_parts.extend(
            [
                "",
                "## Fusion",
                f"Titan score: {_fmt(fusion.get('titan_score'))} · "
                f"fusion confidence {_fmt(fusion.get('overall_confidence'), 3)}",
            ]
        )
        expl = fusion.get("overall_explanation")
        if expl:
            md_parts.append(str(expl))

    return {
        "symbol": symbol,
        "action_signal": signal,
        "titan_score": fusion.get("titan_score"),
        "fusion_confidence": fusion.get("overall_confidence"),
        "sections": sections,
        "outlook": outlook,
        "markdown": "\n".join(md_parts).strip(),
    }
