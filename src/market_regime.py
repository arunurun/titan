"""Market-wide regime detection and threshold adaptations.

Regime classification and adaptations always run. Optional ``TITAN_REGIME_ENGINE_MODE``:
  - ``enforce`` (default): apply buy-threshold / Tier-2 / defensive adaptations.
  - ``shadow``: record would-be adaptations without changing labels/scores.
  - ``off``: classify and record regime only; no adaptations applied.

Regime labels use hysteresis: a change requires either 3 consecutive sessions at the
new raw classification or a 5-point breadth exit band (e.g. STRONG_BULL exits < 55).
"""

from __future__ import annotations

import math
import os
from typing import Any

from score_types import FactorResult

REGIMES: tuple[str, ...] = ("STRONG_BULL", "BULL", "NEUTRAL", "DEFENSIVE", "BEAR")

REGIME_SCORE_MAP: dict[str, float] = {
    "STRONG_BULL": 90.0,
    "BULL": 75.0,
    "NEUTRAL": 50.0,
    "DEFENSIVE": 40.0,
    "BEAR": 25.0,
    "CORRECTION": 40.0,
    "STRONG_BEAR": 15.0,
}

_BUY_THRESHOLD_DELTA_STRONG_BULL = -5.0
_BUY_THRESHOLD_DELTA_BEAR = 5.0
_TIER2_PENALTY_MULT_STRONG_BULL = 0.75
_DEFENSIVE_PENALTY_MULT_BEAR = 1.20

# Sessions raw regime must persist before switching (unless exit band fires).
_REGIME_PERSIST_SESSIONS = 3
# Breadth hysteresis: exit band is entry threshold minus this delta.
_REGIME_BREADTH_HYSTERESIS = 5.0


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


def regime_engine_mode() -> str:
    raw = os.environ.get("TITAN_REGIME_ENGINE_MODE", "").strip().lower()
    return raw if raw in ("off", "shadow", "enforce") else "enforce"


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _exit_band_satisfied(
    prior_regime: str,
    *,
    breadth_pct: float,
    nifty_above_ema200: bool | None,
) -> bool:
    """True when breadth/structure clears the 5-point exit band for ``prior_regime``."""
    breadth = _sf(breadth_pct)
    if math.isnan(breadth):
        return False
    hyst = _env_float("TITAN_REGIME_BREADTH_HYSTERESIS", _REGIME_BREADTH_HYSTERESIS)
    strong_enter = _env_float("TITAN_REGIME_STRONG_BREADTH", 60.0)
    def_enter = _env_float("TITAN_REGIME_DEFENSIVE_BREADTH", 40.0)
    bear_enter = _env_float("TITAN_REGIME_BEAR_BREADTH", 30.0)
    prior = str(prior_regime or "NEUTRAL").strip().upper()
    if prior == "STRONG_BULL":
        return breadth < (strong_enter - hyst)
    if prior == "DEFENSIVE":
        return breadth > (def_enter + hyst)
    if prior == "BEAR":
        return breadth > (bear_enter + hyst) or nifty_above_ema200 is True
    if prior == "BULL":
        return nifty_above_ema200 is False
    return False


def resolve_regime_with_hysteresis(
    raw_regime: str,
    *,
    prior_regime: str | None = None,
    prior_raw_regime: str | None = None,
    regime_streak: int = 0,
    breadth_pct: float = float("nan"),
    nifty_above_ema200: bool | None = None,
) -> dict[str, Any]:
    """Apply persistence / breadth-band hysteresis to a raw regime label."""
    raw = str(raw_regime or "NEUTRAL").strip().upper()
    prior = str(prior_regime or "").strip().upper() or None
    streak = max(0, int(regime_streak or 0))
    persist_needed = _env_int("TITAN_REGIME_PERSIST_SESSIONS", _REGIME_PERSIST_SESSIONS)

    if prior is None:
        return {
            "regime": raw,
            "streak": 1,
            "candidate_streak": 1,
            "hysteresis_applied": False,
            "change_reason": "initial",
        }

    if raw == prior:
        return {
            "regime": prior,
            "streak": streak + 1,
            "candidate_streak": 0,
            "hysteresis_applied": False,
            "change_reason": "unchanged",
        }

    prior_raw = str(prior_raw_regime or "").strip().upper()
    candidate_streak = streak + 1 if prior_raw == raw else 1
    exit_band = _exit_band_satisfied(
        prior,
        breadth_pct=breadth_pct,
        nifty_above_ema200=nifty_above_ema200,
    )

    if candidate_streak >= persist_needed or exit_band:
        reason = "persist" if candidate_streak >= persist_needed else "exit_band"
        return {
            "regime": raw,
            "streak": 1,
            "candidate_streak": candidate_streak,
            "hysteresis_applied": True,
            "change_reason": reason,
        }

    return {
        "regime": prior,
        "streak": candidate_streak,
        "candidate_streak": candidate_streak,
        "hysteresis_applied": True,
        "change_reason": "held_prior",
    }


def detect_market_regime(
    *,
    nifty_above_ema200: bool | None = None,
    breadth_pct: float = float("nan"),
    vix: float = float("nan"),
    nifty_adx: float = float("nan"),
) -> dict[str, Any]:
    """Classify the instantaneous (raw) market regime from macro inputs.

    Rules (first match wins after STRONG_BULL check):
      STRONG_BULL: Nifty above EMA200, breadth > 60, VIX < 18
      DEFENSIVE: breadth < 40
      BEAR: Nifty below EMA200 and breadth < 30
      BULL: Nifty above EMA200 (remaining cases)
      NEUTRAL: otherwise
    """
    breadth = _sf(breadth_pct)
    vix_val = _sf(vix)
    adx = _sf(nifty_adx)
    above = nifty_above_ema200

    reasons: list[str] = []
    regime = "NEUTRAL"

    strong_breadth = _env_float("TITAN_REGIME_STRONG_BREADTH", 60.0)
    strong_vix = _env_float("TITAN_REGIME_STRONG_VIX", 18.0)
    def_breadth = _env_float("TITAN_REGIME_DEFENSIVE_BREADTH", 40.0)
    bear_breadth = _env_float("TITAN_REGIME_BEAR_BREADTH", 30.0)

    if above is True and (not math.isnan(breadth)) and breadth > strong_breadth:
        if math.isnan(vix_val) or vix_val < strong_vix:
            regime = "STRONG_BULL"
            reasons.append(f"nifty>ema200 breadth>{strong_breadth} vix<{strong_vix}")
    if regime == "NEUTRAL" and above is False and (not math.isnan(breadth)) and breadth < bear_breadth:
        regime = "BEAR"
        reasons.append(f"nifty<ema200 breadth<{bear_breadth}")
    elif regime == "NEUTRAL" and (not math.isnan(breadth)) and breadth < def_breadth:
        regime = "DEFENSIVE"
        reasons.append(f"breadth<{def_breadth}")
    elif regime == "NEUTRAL" and above is True:
        regime = "BULL"
        reasons.append("nifty>ema200")
    elif regime == "NEUTRAL":
        reasons.append("default neutral")

    return {
        "regime": regime,
        "nifty_above_ema200": above,
        "breadth_pct": None if math.isnan(breadth) else round(breadth, 2),
        "vix": None if math.isnan(vix_val) else round(vix_val, 2),
        "nifty_adx": None if math.isnan(adx) else round(adx, 2),
        "reasons": reasons,
    }


def regime_adaptations(regime: str) -> dict[str, Any]:
    """Return threshold / penalty multipliers for a regime label."""
    r = str(regime or "NEUTRAL").strip().upper()
    buy_delta = 0.0
    tier2_mult = 1.0
    defensive_mult = 1.0
    ignore_mild_overext = False
    if r == "STRONG_BULL":
        buy_delta = _env_float("TITAN_REGIME_BUY_DELTA_STRONG_BULL", _BUY_THRESHOLD_DELTA_STRONG_BULL)
        tier2_mult = _env_float("TITAN_REGIME_TIER2_MULT_STRONG_BULL", _TIER2_PENALTY_MULT_STRONG_BULL)
        ignore_mild_overext = True
    elif r == "BEAR":
        buy_delta = _env_float("TITAN_REGIME_BUY_DELTA_BEAR", _BUY_THRESHOLD_DELTA_BEAR)
        defensive_mult = _env_float("TITAN_REGIME_DEFENSIVE_MULT_BEAR", _DEFENSIVE_PENALTY_MULT_BEAR)
    return {
        "buy_threshold_delta": buy_delta,
        "tier2_penalty_mult": tier2_mult,
        "defensive_penalty_mult": defensive_mult,
        "ignore_mild_overextension": ignore_mild_overext,
    }


def apply_regime_to_audit(audit: dict[str, Any]) -> dict[str, Any]:
    """Detect regime from audit fields, apply hysteresis, store on audit, apply adaptations."""
    above_raw = audit.get("nifty_above_ema200")
    if above_raw is None:
        ema_dist = _sf(audit.get("nifty_ema200_distance_pct", audit.get("benchmark_ema200_distance_pct")))
        above = (not math.isnan(ema_dist)) and ema_dist > 0.0
    else:
        above = bool(above_raw)

    breadth = _sf(audit.get("market_breadth_pct", audit.get("breadth_above_ema200_pct")))
    detected = detect_market_regime(
        nifty_above_ema200=above,
        breadth_pct=breadth,
        vix=_sf(audit.get("india_vix", audit.get("vix"))),
        nifty_adx=_sf(audit.get("nifty_adx_14", audit.get("benchmark_adx_14"))),
    )
    raw_regime = detected["regime"]

    prior_payload = audit.get("prev_market_regime")
    if isinstance(prior_payload, dict):
        prior_regime = prior_payload.get("regime")
        prior_raw = prior_payload.get("raw_regime")
        prior_streak = int(prior_payload.get("streak") or 0)
    else:
        prior_regime = audit.get("prev_market_regime")
        prior_raw = audit.get("prev_market_regime_raw")
        prior_streak = int(audit.get("prev_market_regime_streak") or 0)

    resolved = resolve_regime_with_hysteresis(
        raw_regime,
        prior_regime=str(prior_regime) if prior_regime else None,
        prior_raw_regime=str(prior_raw) if prior_raw else None,
        regime_streak=prior_streak,
        breadth_pct=breadth,
        nifty_above_ema200=above,
    )
    effective_regime = resolved["regime"]
    adapts = regime_adaptations(effective_regime)
    mode = regime_engine_mode()
    payload: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        **detected,
        "raw_regime": raw_regime,
        "regime": effective_regime,
        "streak": resolved["streak"],
        "candidate_streak": resolved["candidate_streak"],
        "hysteresis": {
            "applied": resolved["hysteresis_applied"],
            "change_reason": resolved["change_reason"],
            "persist_sessions": _env_int("TITAN_REGIME_PERSIST_SESSIONS", _REGIME_PERSIST_SESSIONS),
            "breadth_hysteresis_pts": _env_float(
                "TITAN_REGIME_BREADTH_HYSTERESIS", _REGIME_BREADTH_HYSTERESIS
            ),
        },
        "adaptations": adapts,
        "applied": mode == "enforce",
    }
    audit["market_regime"] = payload

    if mode == "enforce":
        audit["regime_buy_threshold_delta"] = adapts["buy_threshold_delta"]
        audit["regime_tier2_penalty_mult"] = adapts["tier2_penalty_mult"]
        audit["regime_defensive_penalty_mult"] = adapts["defensive_penalty_mult"]
        audit["regime_ignore_mild_overextension"] = adapts["ignore_mild_overextension"]
    else:
        audit["regime_buy_threshold_delta"] = 0.0
        audit["regime_tier2_penalty_mult"] = 1.0
        audit["regime_defensive_penalty_mult"] = 1.0
        audit["regime_ignore_mild_overextension"] = False

    return payload


def _regime_breadth_blend() -> float:
    return _env_float("TITAN_REGIME_BREADTH_BLEND", 0.30)


def score_market_regime_context(
    audit: dict[str, Any],
    *,
    regime_label: str | None = None,
    breadth_score: float | None = None,
) -> FactorResult:
    """
    Regime factor with discrete label map and optional breadth blend.
    Breadth is diagnostic input to regime_score — not a fusion pillar weight.
    """
    label = str(regime_label or "").strip().upper()
    if not label:
        payload = audit.get("market_regime")
        if isinstance(payload, dict):
            label = str(payload.get("regime") or payload.get("raw_regime") or "NEUTRAL").upper()
        else:
            label = "NEUTRAL"

    rule_score = REGIME_SCORE_MAP.get(label, REGIME_SCORE_MAP["NEUTRAL"])
    breadth_raw = breadth_score
    if breadth_raw is None:
        breadth_raw = _sf(audit.get("market_breadth_pct", audit.get("breadth_above_ema200_pct")))
        if math.isnan(breadth_raw):
            bf = audit.get("breadth")
            if isinstance(bf, dict) and bf.get("score") is not None:
                breadth_raw = _sf(bf.get("score"))

    blend = _regime_breadth_blend()
    if breadth_raw is not None and not math.isnan(float(breadth_raw)):
        b = float(breadth_raw)
        score = round((1.0 - blend) * rule_score + blend * b, 2)
        reasons = [f"regime={label}", f"breadth blend {blend:.0%}"]
        meta = {
            "regime_label": label,
            "rule_score": rule_score,
            "breadth_score": round(b, 2),
            "breadth_blend": blend,
        }
    else:
        score = round(rule_score, 2)
        reasons = [f"regime={label}"]
        meta = {"regime_label": label, "rule_score": rule_score, "breadth_blend": blend}

    return {
        "score": score,
        "confidence": 0.8 if label != "NEUTRAL" else 0.65,
        "reasons": reasons,
        "metadata": meta,
        "available": True,
    }
