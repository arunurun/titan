"""Multi-horizon stretch metrics (Phase 5).

When ``TITAN_NEW_STRETCH_ENGINE=True``, composite stretch replaces EMA200-only stretch
in over-extension scoring paths. Legacy behaviour preserved when flag is off.
"""

from __future__ import annotations

import math
import os
from typing import Any

import pandas as pd

from titan_engine import calculate_atr, calculate_ema


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def new_stretch_engine_enabled() -> bool:
    return _env_truthy("TITAN_NEW_STRETCH_ENGINE", default=False)


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _ema_distance_pct(close: float, ema: float) -> float:
    if math.isnan(close) or math.isnan(ema) or ema == 0.0:
        return float("nan")
    return ((close / ema) - 1.0) * 100.0


def _stretch_atr(distance_pct: float, atr_pct: float) -> float:
    if math.isnan(distance_pct) or math.isnan(atr_pct) or atr_pct == 0.0:
        return float("nan")
    return distance_pct / atr_pct


def compute_stretch_metrics(df: pd.DataFrame) -> dict[str, float]:
    """Return ema20/ema50 stretch ATR units and distance from 52-week high (%)."""
    out = {
        "ema20_stretch_atr": float("nan"),
        "ema50_stretch_atr": float("nan"),
        "distance_from_52w_high_pct": float("nan"),
        "stretch_composite": float("nan"),
    }
    if df is None or df.empty:
        return out
    close_col = "close" if "close" in df.columns else df.columns[-1]
    series = pd.to_numeric(df[close_col], errors="coerce").dropna()
    if series.empty:
        return out
    close_last = float(series.iloc[-1])
    atr_14 = calculate_atr(df, window=14)
    atr_pct = (atr_14 / close_last * 100.0) if close_last != 0.0 and not math.isnan(atr_14) else float("nan")

    ema20 = calculate_ema(series, span=20)
    ema50 = calculate_ema(series, span=50)
    d20 = _ema_distance_pct(close_last, ema20)
    d50 = _ema_distance_pct(close_last, ema50)
    out["ema20_stretch_atr"] = _stretch_atr(d20, atr_pct)
    out["ema50_stretch_atr"] = _stretch_atr(d50, atr_pct)

    lookback = min(252, len(series))
    high_52w = float(series.iloc[-lookback:].max())
    if high_52w > 0:
        out["distance_from_52w_high_pct"] = ((close_last / high_52w) - 1.0) * 100.0

    out["stretch_composite"] = compute_stretch_composite(
        ema20_stretch_atr=out["ema20_stretch_atr"],
        ema50_stretch_atr=out["ema50_stretch_atr"],
        distance_from_52w_high_pct=out["distance_from_52w_high_pct"],
    )
    return out


def compute_stretch_composite(
    *,
    ema20_stretch_atr: float,
    ema50_stretch_atr: float,
    distance_from_52w_high_pct: float,
) -> float:
    """Weighted composite stretch (positive = extended above means / near highs)."""
    e20 = _sf(ema20_stretch_atr)
    e50 = _sf(ema50_stretch_atr)
    d52 = _sf(distance_from_52w_high_pct)
    # Map 52w distance: at high -> 0%, far below -> negative; invert to stretch-like units.
    d52_stretch = -d52 / 5.0 if not math.isnan(d52) else float("nan")
    terms: list[tuple[float, float]] = [
        (0.5, e20),
        (0.3, e50),
        (0.2, d52_stretch),
    ]
    total_w = 0.0
    acc = 0.0
    for w, v in terms:
        if math.isnan(v):
            continue
        acc += w * v
        total_w += w
    if total_w == 0.0:
        return float("nan")
    return round(acc / total_w, 4)


def effective_stretch_atr(audit: dict[str, Any]) -> float:
    """Return stretch input for over-extension scoring (legacy or composite)."""
    if new_stretch_engine_enabled():
        comp = _sf(audit.get("stretch_composite"))
        if not math.isnan(comp):
            return comp
    return _sf(audit.get("ema200_stretch_atr"))
