"""Market-wide regime detection and threshold adaptations.

Validation modes (legacy / shadow / enforce) apply via ``TITAN_REGIME_ENGINE_MODE``:
  - ``off`` or unset: classify only when ``TITAN_ENABLE_REGIME_ENGINE`` is true; no adaptations.
  - ``shadow``: record would-be adaptations on the audit without changing labels/scores.
  - ``enforce``: apply adaptations to buy thresholds and Tier-2 / defensive scaling.

Feature flag ``TITAN_ENABLE_REGIME_ENGINE`` must be true for any regime logic to run.
"""

from __future__ import annotations

import math
import os
from typing import Any

REGIMES: tuple[str, ...] = ("STRONG_BULL", "BULL", "NEUTRAL", "DEFENSIVE", "BEAR")

_BUY_THRESHOLD_DELTA_STRONG_BULL = -5.0
_BUY_THRESHOLD_DELTA_BEAR = 5.0
_TIER2_PENALTY_MULT_STRONG_BULL = 0.75
_DEFENSIVE_PENALTY_MULT_BEAR = 1.20


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


def regime_engine_enabled() -> bool:
    return _env_truthy("TITAN_ENABLE_REGIME_ENGINE", default=False)


def regime_engine_mode() -> str:
    raw = os.environ.get("TITAN_REGIME_ENGINE_MODE", "").strip().lower()
    return raw if raw in ("off", "shadow", "enforce") else "shadow"


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def detect_market_regime(
    *,
    nifty_above_ema200: bool | None = None,
    breadth_pct: float = float("nan"),
    vix: float = float("nan"),
    nifty_adx: float = float("nan"),
) -> dict[str, Any]:
    """Classify the current market regime from macro inputs.

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
    """Detect regime from audit fields, store on audit, optionally apply adaptations."""
    if not regime_engine_enabled():
        audit["market_regime"] = {"enabled": False}
        return {"enabled": False}

    above_raw = audit.get("nifty_above_ema200")
    if above_raw is None:
        ema_dist = _sf(audit.get("nifty_ema200_distance_pct", audit.get("benchmark_ema200_distance_pct")))
        above = (not math.isnan(ema_dist)) and ema_dist > 0.0
    else:
        above = bool(above_raw)

    detected = detect_market_regime(
        nifty_above_ema200=above,
        breadth_pct=_sf(audit.get("market_breadth_pct", audit.get("breadth_above_ema200_pct"))),
        vix=_sf(audit.get("india_vix", audit.get("vix"))),
        nifty_adx=_sf(audit.get("nifty_adx_14", audit.get("benchmark_adx_14"))),
    )
    adapts = regime_adaptations(detected["regime"])
    mode = regime_engine_mode()
    payload: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        **detected,
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
