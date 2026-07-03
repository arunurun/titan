"""Institutional flow factor — reads CMF/OBV from audit; never recomputes them."""

from __future__ import annotations

import math
from typing import Any

from score_types import FactorResult


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_institutional_flow(
    audit: dict[str, Any],
    *,
    delivery_pct: float | None = None,
) -> FactorResult:
    """
    Flow accumulation score from audit CMF/OBV fields and optional delivery %.
    Does not recompute CMF or OBV — reads precomputed audit values only.
    """
    cmf = _sf(audit.get("cmf_20"))
    obv_slope = _sf(audit.get("obv_slope_20"))
    obv_confirm = audit.get("obv_trend_confirm")
    vpr = _sf(audit.get("volume_participation_ratio", audit.get("absorption_ratio")))
    cmf_delta = audit.get("cmf_20_delta")
    delta_interp = ""
    if isinstance(cmf_delta, dict):
        delta_interp = str(cmf_delta.get("interpretation") or "")

    parts: list[float] = []
    weights: list[float] = []
    reasons: list[str] = []
    meta: dict[str, Any] = {"source": "audit_tape"}

    if not math.isnan(cmf):
        meta["cmf_20"] = round(cmf, 4)
        cmf_score = _clamp(50.0 + cmf * 80.0)
        parts.append(cmf_score)
        weights.append(0.35)
        if cmf > 0.05:
            reasons.append(f"CMF20 positive ({cmf:.3f})")
        elif cmf < -0.05:
            reasons.append(f"CMF20 negative ({cmf:.3f})")

    if not math.isnan(obv_slope):
        meta["obv_slope_20"] = round(obv_slope, 4)
        slope_norm = _clamp(50.0 + obv_slope * 5.0)
        parts.append(slope_norm)
        weights.append(0.25)
        if obv_slope > 0:
            reasons.append("OBV slope rising")

    if obv_confirm is True:
        meta["obv_trend_confirm"] = True
        parts.append(65.0)
        weights.append(0.15)
        reasons.append("OBV trend confirmed")
    elif obv_confirm is False:
        meta["obv_trend_confirm"] = False
        parts.append(40.0)
        weights.append(0.10)

    # Volume spike accumulation: high participation on up days boosts flow read.
    ret1d = _sf(audit.get("return_1d_pct"))
    if not math.isnan(vpr) and not math.isnan(ret1d):
        meta["volume_participation_ratio"] = round(vpr, 4)
        if vpr >= 1.5 and ret1d > 0:
            spike_score = _clamp(55.0 + min(25.0, (vpr - 1.0) * 15.0 + ret1d * 0.5))
            parts.append(spike_score)
            weights.append(0.20)
            reasons.append("accumulation on volume spike")
        elif vpr >= 1.5 and ret1d < 0:
            dist_score = _clamp(45.0 - min(20.0, (vpr - 1.0) * 10.0))
            parts.append(dist_score)
            weights.append(0.15)
            reasons.append("distribution on volume spike")

    if delta_interp == "strengthening":
        parts.append(62.0)
        weights.append(0.10)
        reasons.append("CMF delta strengthening")
    elif delta_interp == "weakening":
        parts.append(42.0)
        weights.append(0.10)
        reasons.append("CMF delta weakening")

    deliv = _sf(delivery_pct) if delivery_pct is not None else float("nan")
    if math.isnan(deliv):
        deliv = _sf(audit.get("delivery_pct"))
    if not math.isnan(deliv):
        meta["delivery_pct"] = round(deliv, 2)
        deliv_score = _clamp(40.0 + deliv * 0.6)
        parts.append(deliv_score)
        weights.append(0.15)
        reasons.append(f"delivery {deliv:.0f}%")

    legacy = audit.get("institutional_flow")
    if isinstance(legacy, dict) and legacy.get("available") and legacy.get("score") is not None:
        meta["legacy_institutional_flow"] = True
        ls = _sf(legacy.get("score"))
        if not math.isnan(ls):
            parts.append(_clamp(ls))
            weights.append(0.30)

    if not parts:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["CMF/OBV flow inputs missing"],
            "metadata": meta,
            "available": False,
        }

    total_w = sum(weights) if weights else float(len(parts))
    if total_w <= 0:
        score = sum(parts) / len(parts)
    else:
        score = sum(p * w for p, w in zip(parts, weights)) / total_w

    return {
        "score": round(_clamp(score), 2),
        "confidence": round(min(1.0, 0.45 + 0.1 * len(parts)), 3),
        "reasons": reasons[:5] if reasons else ["flow composite"],
        "metadata": meta,
        "available": True,
    }
