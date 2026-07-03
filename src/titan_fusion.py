"""Titan Fusion Engine — weighted composite of independent factor scores.

Sits between independent engines and prediction/signal layers. Does not emit
BUY/SELL/HOLD; that remains the existing action/signal pipeline.

Composite layer only — do not create titan_score.py; use fuse_from_audit() here.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, TypedDict

from score_types import FactorResult

FUSION_PILLARS: tuple[str, ...] = (
    "technical",
    "relative_strength",
    "institutional_flow",
    "fundamentals",
    "market_regime",
    "sector_strength",
    "risk",
)

DEFAULT_FUSION_WEIGHTS: dict[str, float] = {
    "technical": 0.30,
    "relative_strength": 0.20,
    "institutional_flow": 0.15,
    "fundamentals": 0.15,
    "market_regime": 0.10,
    "sector_strength": 0.05,
    "risk": 0.05,
}

DISPLAY_LABELS: dict[str, str] = {
    "technical": "Technical",
    "relative_strength": "Relative Strength",
    "institutional_flow": "Flow",
    "fundamentals": "Fundamentals",
    "market_regime": "Regime",
    "sector_strength": "Sector",
    "risk": "Risk",
}

# Discrete regime label → 0–100 score. DEFENSIVE matches market_regime.py; CORRECTION/STRONG_BEAR
# are legacy aliases for reports and external feeds.
REGIME_SCORE_MAP: dict[str, float] = {
    "STRONG_BULL": 90.0,
    "BULL": 75.0,
    "NEUTRAL": 50.0,
    "DEFENSIVE": 40.0,
    "BEAR": 25.0,
    "CORRECTION": 40.0,
    "STRONG_BEAR": 15.0,
}

_ENGINE_VERSION = "fusion_v1"
_WEIGHT_TOLERANCE = 0.001


class FusionComponent(TypedDict):
    key: str
    score: float | None
    confidence: float
    reason: str
    available: bool
    metadata: dict[str, Any]


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


_DEFAULT_CALIBRATED_WEIGHTS_PATH = "data/calibration/recommended_weights.json"

_RECOMMENDED_WEIGHTS_SEARCH_PATHS: tuple[str, ...] = (
    "data/recommended_weights.json",
    "config/recommended_weights.json",
    "data/calibration/recommended_weights.json",
)


def _pillar_env_key(pillar: str) -> str:
    return f"TITAN_FUSION_WEIGHT_{pillar.upper()}"


def _load_calibrated_weights_json(path: str) -> dict[str, float] | None:
    try:
        p = Path(path)
        if not p.is_file():
            return None
        parsed = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            return None
        weights_block = parsed.get("weights") if "weights" in parsed else parsed
        if not isinstance(weights_block, dict):
            return None
        out: dict[str, float] = {}
        for key, value in weights_block.items():
            k = str(key).strip().lower()
            if k in FUSION_PILLARS:
                out[k] = float(value)
        return out or None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _resolve_recommended_weights_path() -> str | None:
    env_path = os.environ.get("TITAN_FUSION_CALIBRATED_WEIGHTS_PATH", "").strip()
    if env_path:
        return env_path
    for rel in _RECOMMENDED_WEIGHTS_SEARCH_PATHS:
        candidate = Path(rel)
        if candidate.is_file():
            return str(candidate)
    return None


def load_fusion_weights(
    *,
    sector_key: str | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Resolve fusion weights: calibrated JSON → JSON env → per-key env → defaults."""
    del sector_key, audit  # reserved for future sector-specific hooks

    weights = dict(DEFAULT_FUSION_WEIGHTS)
    normalized_from_config = False
    warning: str | None = None

    if _env_flag("TITAN_FUSION_USE_CALIBRATED_WEIGHTS", default=False):
        cal_path = _resolve_recommended_weights_path() or _DEFAULT_CALIBRATED_WEIGHTS_PATH
        calibrated = _load_calibrated_weights_json(cal_path)
        if calibrated:
            weights.update(calibrated)
        else:
            warning = f"calibrated weights unavailable at {cal_path}"
    elif _resolve_recommended_weights_path():
        cal_path = _resolve_recommended_weights_path()
        if cal_path:
            calibrated = _load_calibrated_weights_json(cal_path)
            if calibrated:
                weights.update(calibrated)

    raw_json = os.environ.get("TITAN_FUSION_WEIGHTS_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    k = str(key).strip().lower()
                    if k in FUSION_PILLARS:
                        weights[k] = float(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            warning = "invalid TITAN_FUSION_WEIGHTS_JSON"

    for pillar in FUSION_PILLARS:
        env_name = _pillar_env_key(pillar)
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            weights[pillar] = float(raw)
        except ValueError:
            continue

    total = sum(weights[p] for p in FUSION_PILLARS)
    if total > 0 and abs(total - 1.0) > _WEIGHT_TOLERANCE:
        weights = {p: weights[p] / total for p in FUSION_PILLARS}
        normalized_from_config = True

    if normalized_from_config or warning:
        weights = {
            **weights,
            **{
                "_fusion_weight_meta": {
                    "normalized": normalized_from_config,
                    "warning": warning,
                }
            },
        }
    return weights


def _clean_weights(weights: dict[str, float] | None) -> dict[str, float]:
    base = dict(DEFAULT_FUSION_WEIGHTS)
    if weights:
        for pillar in FUSION_PILLARS:
            if pillar in weights:
                base[pillar] = float(weights[pillar])
    total = sum(base[p] for p in FUSION_PILLARS)
    if total > 0 and abs(total - 1.0) > _WEIGHT_TOLERANCE:
        base = {p: base[p] / total for p in FUSION_PILLARS}
    return base


def _make_component(
    key: str,
    *,
    score: float | None,
    confidence: float = 0.7,
    reason: str = "",
    available: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> FusionComponent:
    is_available = available if available is not None else score is not None
    return {
        "key": key,
        "score": round(score, 2) if score is not None and not math.isnan(score) else None,
        "confidence": _clamp(confidence, 0.0, 1.0),
        "reason": reason,
        "available": bool(is_available and score is not None),
        "metadata": dict(metadata or {}),
    }


def _factor_to_component(key: str, factor: FactorResult | dict[str, Any]) -> FusionComponent:
    score_raw = factor.get("score")
    score: float | None
    if score_raw is None:
        score = None
    else:
        try:
            score = float(score_raw)
            if math.isnan(score):
                score = None
        except (TypeError, ValueError):
            score = None

    reasons = factor.get("reasons") or []
    reason = str(reasons[0]) if reasons else ""
    available = bool(factor.get("available", False)) and score is not None

    return {
        "key": key,
        "score": round(score, 2) if score is not None else None,
        "confidence": _clamp(_sf(factor.get("confidence", 0.7)), 0.0, 1.0),
        "reason": reason,
        "available": available,
        "metadata": dict(factor.get("metadata") or {}),
    }


def _active_keys(
    components: dict[str, FusionComponent],
    config_weights: dict[str, float],
) -> list[str]:
    active: list[str] = []
    for pillar in FUSION_PILLARS:
        comp = components.get(pillar)
        if comp is None:
            continue
        if not comp.get("available"):
            continue
        if comp.get("score") is None:
            continue
        if pillar not in config_weights:
            continue
        active.append(pillar)
    return active


def _effective_weights(
    config_weights: dict[str, float],
    active: list[str],
) -> dict[str, float]:
    if not active:
        return {}
    active_mass = sum(config_weights[k] for k in active)
    if active_mass <= 0:
        share = 1.0 / len(active)
        return {k: share for k in active}
    missing_mass = 1.0 - active_mass
    eff: dict[str, float] = {}
    for k in active:
        eff[k] = config_weights[k] + (config_weights[k] / active_mass) * missing_mass
    return eff


def _weighted_std(scores: list[float], weights: list[float]) -> float:
    if not scores:
        return 0.0
    total_w = sum(weights)
    if total_w <= 0:
        return 0.0
    mean = sum(s * w for s, w in zip(scores, weights)) / total_w
    var = sum(w * (s - mean) ** 2 for s, w in zip(scores, weights)) / total_w
    return math.sqrt(max(0.0, var))


def _aggregate_confidence(
    components: dict[str, FusionComponent],
    eff_weights: dict[str, float],
    active: list[str],
    coverage: float,
) -> float:
    if not active:
        return 0.0

    base = sum(
        eff_weights[k] * components[k]["confidence"]
        for k in active
    )
    coverage_factor = 0.5 + 0.5 * coverage

    dispersion_factor = 1.0
    if _env_flag("TITAN_FUSION_CONF_DISPERSION", default=True):
        scores = [float(components[k]["score"]) for k in active]
        wts = [eff_weights[k] for k in active]
        w_std = _weighted_std(scores, wts)
        dispersion_factor = 1.0 - 0.3 * _clamp(w_std / 50.0, 0.0, 1.0)

    return round(_clamp(base * coverage_factor * dispersion_factor, 0.0, 1.0), 3)


def fuse_titan_score(
    components: dict[str, FusionComponent],
    *,
    weights: dict[str, float] | None = None,
    breadth: FusionComponent | None = None,
) -> dict[str, Any]:
    """Pure fusion. No I/O. Breadth is diagnostic only — not weighted in titan_score."""
    config_weights = _clean_weights(weights)
    active = _active_keys(components, config_weights)
    missing = [p for p in FUSION_PILLARS if p not in active]
    coverage = sum(config_weights[k] for k in active)

    eff_weights = _effective_weights(config_weights, active)

    contributions: dict[str, dict[str, Any]] = {}
    titan_score: float | None = None
    overall_confidence = 0.0
    overall_explanation = "no fusion components available"

    if active:
        raw_total = 0.0
        for k in active:
            score = float(components[k]["score"])
            w_eff = eff_weights[k]
            weighted = score * w_eff
            raw_total += weighted
            contributions[k] = {
                "score": score,
                "weight_config": config_weights[k],
                "weight_effective": round(w_eff, 6),
                "weighted": round(weighted, 2),
            }
        titan_score = round(raw_total, 1)
        overall_confidence = _aggregate_confidence(components, eff_weights, active, coverage)
        overall_explanation = format_fusion_explanation(
            {
                "titan_score": titan_score,
                "contributions": contributions,
                "weights": {
                    "missing_keys": missing,
                    "redistribution": "proportional",
                },
            }
        )

    breadth_score: float | None = None
    breadth_out: FusionComponent | None = None
    if breadth is not None:
        breadth_out = breadth
        if breadth.get("score") is not None:
            breadth_score = float(breadth["score"])

    pillar_scores = {
        "technical_score": _pillar_score(components, "technical"),
        "relative_strength_score": _pillar_score(components, "relative_strength"),
        "flow_score": _pillar_score(components, "institutional_flow"),
        "fundamental_score": _pillar_score(components, "fundamentals"),
        "regime_score": _pillar_score(components, "market_regime"),
        "sector_score": _pillar_score(components, "sector_strength"),
        "risk_score": _pillar_score(components, "risk"),
    }

    return {
        "titan_score": titan_score,
        **pillar_scores,
        "breadth_score": breadth_score,
        "overall_confidence": overall_confidence,
        "overall_explanation": overall_explanation,
        "components": {k: components[k] for k in FUSION_PILLARS if k in components},
        "breadth": breadth_out,
        "contributions": contributions,
        "weights": {
            "config": config_weights,
            "effective": eff_weights,
            "active_keys": active,
            "missing_keys": missing,
            "redistribution": "proportional",
        },
        "coverage": round(coverage, 4),
        "engine_version": _ENGINE_VERSION,
        "expected_return_hint": None,
        "metadata": {
            "n_active": len(active),
            "n_total": len(FUSION_PILLARS),
        },
    }


def _pillar_score(components: dict[str, FusionComponent], key: str) -> float | None:
    comp = components.get(key)
    if comp is None or comp.get("score") is None:
        return None
    return float(comp["score"])


def format_fusion_explanation(result: dict[str, Any]) -> str:
    """Human-readable breakdown matching the user example format."""
    contributions = result.get("contributions") or {}
    lines: list[str] = []
    for pillar in FUSION_PILLARS:
        row = contributions.get(pillar)
        if not row:
            continue
        label = DISPLAY_LABELS.get(pillar, pillar)
        score = row["score"]
        w_pct = int(round(float(row["weight_effective"]) * 100))
        weighted = row["weighted"]
        lines.append(f"{label}\n{score:.0f}\n×\n{w_pct}%\n=\n{weighted:.1f}")

    titan = result.get("titan_score")
    if titan is not None:
        lines.append(f"Total\n{titan:.1f}")

    weights_block = result.get("weights") or {}
    missing = weights_block.get("missing_keys") or []
    if missing:
        names = ", ".join(DISPLAY_LABELS.get(k, k) for k in missing)
        lines.append(f"(Note: {names} unavailable — weights redistributed proportionally)")

    return "\n".join(lines)


def _regime_label(audit: dict[str, Any]) -> str:
    payload = audit.get("market_regime")
    if isinstance(payload, dict):
        raw = payload.get("regime") or payload.get("raw_regime")
        if raw:
            return str(raw).strip().upper()
    if isinstance(payload, str) and payload.strip():
        return payload.strip().upper()
    return "NEUTRAL"


def _adapt_technical(audit: dict[str, Any]) -> FusionComponent:
    raw = audit.get("effective_intent_score", audit.get("equity_technical_score"))
    score = _sf(raw)
    if math.isnan(score):
        return _make_component("technical", score=None, available=False, reason="technical score missing")
    return _make_component(
        "technical",
        score=_clamp(score, 0.0, 100.0),
        confidence=0.85,
        reason="effective_intent_score",
        metadata={"source": "effective_intent_score"},
    )


def _rel_return_to_score(rel20: float) -> float:
    return _clamp(50.0 + rel20 * 2.5, 0.0, 100.0)


def _adapt_relative_strength(audit: dict[str, Any]) -> FusionComponent:
    parts: list[float] = []
    pctile = _sf(audit.get("sector_relative_strength_pctile"))
    rel20 = _sf(audit.get("rel_return_20d_vs_nifty_pct"))
    if not math.isnan(pctile):
        parts.append(_clamp(pctile, 0.0, 100.0))
    if not math.isnan(rel20):
        parts.append(_rel_return_to_score(rel20))
    if not parts:
        return _make_component(
            "relative_strength",
            score=None,
            available=False,
            reason="relative strength inputs missing",
        )
    score = sum(parts) / len(parts)
    return _make_component(
        "relative_strength",
        score=score,
        confidence=0.75 if len(parts) > 1 else 0.65,
        reason="sector_relative_strength_pctile" if not math.isnan(pctile) else "rel_return_20d_vs_nifty_pct",
        metadata={"n_inputs": len(parts)},
    )


def _adapt_institutional_flow(audit: dict[str, Any]) -> FusionComponent:
    flow = audit.get("institutional_flow")
    if not isinstance(flow, dict) or not flow.get("available"):
        return _make_component(
            "institutional_flow",
            score=None,
            available=False,
            reason="institutional flow unavailable",
        )
    score_raw = flow.get("score")
    if score_raw is None:
        return _make_component(
            "institutional_flow",
            score=None,
            available=False,
            reason="institutional flow score missing",
        )
    score = _sf(score_raw)
    if math.isnan(score):
        return _make_component("institutional_flow", score=None, available=False)
    return _make_component(
        "institutional_flow",
        score=_clamp(score, 0.0, 100.0),
        confidence=_clamp(_sf(flow.get("confidence", 0.7)), 0.3, 1.0),
        reason="institutional_flow",
        metadata={"source": flow.get("source")},
    )


def _adapt_fundamentals(audit: dict[str, Any]) -> FusionComponent:
    score = _sf(audit.get("fundamental_score"))
    if math.isnan(score):
        return _make_component(
            "fundamentals",
            score=None,
            available=False,
            reason="fundamental_score missing",
        )
    return _make_component(
        "fundamentals",
        score=_clamp(score, 0.0, 100.0),
        confidence=0.7,
        reason=str(audit.get("fundamental_status") or "fundamental_score"),
    )


def _adapt_market_regime(audit: dict[str, Any]) -> FusionComponent:
    label = _regime_label(audit)
    score = REGIME_SCORE_MAP.get(label, REGIME_SCORE_MAP["NEUTRAL"])
    return _make_component(
        "market_regime",
        score=score,
        confidence=0.8,
        reason=f"regime={label}",
        metadata={"regime_label": label},
    )


def _adapt_sector_strength(audit: dict[str, Any]) -> FusionComponent:
    pctile = _sf(audit.get("sector_relative_strength_pctile"))
    if math.isnan(pctile):
        return _make_component(
            "sector_strength",
            score=None,
            available=False,
            reason="sector_relative_strength_pctile missing",
        )
    return _make_component(
        "sector_strength",
        score=_clamp(pctile, 0.0, 100.0),
        confidence=0.75,
        reason="sector_relative_strength_pctile",
    )


def _adapt_risk(audit: dict[str, Any]) -> FusionComponent:
    """Defensive proxies from audit — does not use post-signal risk_net."""
    score = 100.0
    penalties = 0
    reasons: list[str] = []

    if audit.get("trap_exit_proxy"):
        score -= 25.0
        penalties += 1
        reasons.append("trap_exit_proxy")
    if audit.get("high_volume_down_day_proxy"):
        score -= 15.0
        penalties += 1
        reasons.append("high_volume_down_day_proxy")
    if audit.get("event_risk_soon"):
        score -= 10.0
        penalties += 1
        reasons.append("event_risk_soon")
    atr = _sf(audit.get("atr_14_pct"))
    med_atr = _sf(audit.get("sector_median_atr_14_pct"))
    if not math.isnan(atr) and not math.isnan(med_atr) and med_atr > 0 and atr > med_atr * 1.5:
        score -= 10.0
        penalties += 1
        reasons.append("elevated_atr_vs_sector")
    if audit.get("history_lt_200_sessions"):
        score -= 10.0
        penalties += 1
        reasons.append("history_lt_200_sessions")
    if audit.get("liquidity_thin_proxy"):
        score -= 15.0
        penalties += 1
        reasons.append("liquidity_thin_proxy")

    score = _clamp(score, 0.0, 100.0)
    confidence = max(0.3, 1.0 - 0.1 * penalties)
    return _make_component(
        "risk",
        score=score,
        confidence=confidence,
        reason="; ".join(reasons) if reasons else "clean risk profile",
        metadata={"penalties_applied": penalties},
    )


def _adapt_breadth(audit: dict[str, Any]) -> FusionComponent | None:
    raw = audit.get("market_breadth_pct", audit.get("breadth_above_ema200_pct"))
    score = _sf(raw)
    if math.isnan(score):
        return None
    return _make_component(
        "breadth",
        score=_clamp(score, 0.0, 100.0),
        confidence=0.7,
        reason="market breadth",
        metadata={"diagnostic": True},
    )


_ADAPTERS: dict[str, Any] = {
    "technical": _adapt_technical,
    "relative_strength": _adapt_relative_strength,
    "institutional_flow": _adapt_institutional_flow,
    "fundamentals": _adapt_fundamentals,
    "market_regime": _adapt_market_regime,
    "sector_strength": _adapt_sector_strength,
    "risk": _adapt_risk,
}


def _build_components_from_audit(audit: dict[str, Any]) -> dict[str, FusionComponent]:
    factor_scores = audit.get("factor_scores")
    out: dict[str, FusionComponent] = {}
    for pillar in FUSION_PILLARS:
        if isinstance(factor_scores, dict) and pillar in factor_scores:
            factor = factor_scores[pillar]
            if isinstance(factor, dict):
                out[pillar] = _factor_to_component(pillar, factor)
                continue
        out[pillar] = _ADAPTERS[pillar](audit)
    return out


def fuse_from_audit(
    audit: dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
    components: dict[str, FusionComponent] | None = None,
) -> dict[str, Any]:
    """Build components from audit factor_scores or adapters; then fuse."""
    resolved_weights = _clean_weights(weights or load_fusion_weights(audit=audit))
    built = components or _build_components_from_audit(audit)
    breadth = _adapt_breadth(audit)
    return fuse_titan_score(built, weights=resolved_weights, breadth=breadth)


def fuse_batch(
    rows: list[dict[str, FusionComponent]],
    *,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Vectorized backtest path — fuse each row of pillar components in batch."""
    resolved = _clean_weights(weights)
    return [fuse_titan_score(row, weights=resolved) for row in rows]


def fusion_enabled() -> bool:
    """When false, fusion is skipped (walk-forward baseline arm)."""
    return _env_flag("TITAN_FUSION_ENABLED", default=True)


def apply_fusion_to_audit(audit: dict[str, Any]) -> dict[str, Any] | None:
    """Orchestrator hook — fuses and stamps audit fusion fields when enabled."""
    if not fusion_enabled():
        return None
    result = fuse_from_audit(audit)
    audit["titan_fusion"] = result
    if result.get("titan_score") is not None:
        audit["titan_score"] = result["titan_score"]
    audit["fusion_confidence"] = result.get("overall_confidence", 0.0)
    return result
