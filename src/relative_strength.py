"""Relative strength factor — stock vs NIFTY and sector benchmarks."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from score_types import FactorResult
from tape_metrics import benchmark_relative_returns, percentile_rank_0_100


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _rel_return_to_score(rel_pct: float) -> float:
    return _clamp(50.0 + rel_pct * 2.5)


def _closes_series(closes: pd.Series | list[float] | None) -> pd.Series:
    if closes is None:
        return pd.Series(dtype=float)
    if isinstance(closes, pd.Series):
        return pd.to_numeric(closes, errors="coerce").dropna()
    return pd.to_numeric(pd.Series(list(closes)), errors="coerce").dropna()


def _bench_df_from_closes(closes: pd.Series | list[float] | None) -> pd.DataFrame | None:
    s = _closes_series(closes)
    if s.empty:
        return None
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=len(s), freq="B")
    return pd.DataFrame({"date": dates, "close": s.values})


def score_relative_strength(
    stock_closes: pd.Series | list[float],
    nifty_closes: pd.Series | list[float] | None,
    sector_closes: pd.Series | list[float] | None,
    *,
    horizons: tuple[int, ...] = (20, 50, 90),
) -> FactorResult:
    """RS vs NIFTY and sector over multiple horizons; composite 0–100 score."""
    stock_df = _bench_df_from_closes(stock_closes)
    if stock_df is None or stock_df.empty:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["insufficient stock history"],
            "metadata": {},
            "available": False,
        }

    nifty_df = _bench_df_from_closes(nifty_closes)
    sector_df = _bench_df_from_closes(sector_closes)

    rel_nifty = benchmark_relative_returns(
        stock_df,
        nifty_df,
        "close",
        horizons=horizons,
    )
    rel_sector: dict[str, float] = {}
    if sector_df is not None:
        raw = benchmark_relative_returns(stock_df, sector_df, "close", horizons=horizons)
        for n in horizons:
            key = f"rel_return_{n}d_vs_nifty_pct"
            sk = f"rel_return_{n}d_vs_sector_pct"
            rel_sector[sk] = raw.get(key, float("nan"))

    parts: list[float] = []
    reasons: list[str] = []
    meta: dict[str, Any] = {"horizons": list(horizons)}
    for n in horizons:
        nk = f"rel_return_{n}d_vs_nifty_pct"
        sk = f"rel_return_{n}d_vs_sector_pct"
        rn = _sf(rel_nifty.get(nk))
        rs = _sf(rel_sector.get(sk))
        meta[nk] = None if math.isnan(rn) else round(rn, 4)
        meta[sk] = None if math.isnan(rs) else round(rs, 4)
        if not math.isnan(rn):
            parts.append(_rel_return_to_score(rn))
            reasons.append(f"nifty {n}d RS {rn:+.1f}%")
        if not math.isnan(rs):
            parts.append(_rel_return_to_score(rs))
            reasons.append(f"sector {n}d RS {rs:+.1f}%")

    if not parts:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["relative return inputs unavailable"],
            "metadata": meta,
            "available": False,
        }

    # Momentum: short vs long RS spread when both 20d and 90d vs NIFTY exist.
    r20 = _sf(rel_nifty.get("rel_return_20d_vs_nifty_pct"))
    r90 = _sf(rel_nifty.get("rel_return_90d_vs_nifty_pct"))
    momentum: float | None = None
    if not math.isnan(r20) and not math.isnan(r90):
        momentum = round(r20 - r90, 4)
        meta["momentum_20d_minus_90d"] = momentum
        if momentum > 2.0:
            parts.append(min(100.0, 55.0 + momentum))
            reasons.append("RS momentum improving")
        elif momentum < -2.0:
            parts.append(max(0.0, 45.0 + momentum))
            reasons.append("RS momentum fading")

    score = round(sum(parts) / len(parts), 2)
    confidence = min(1.0, 0.45 + 0.12 * len(parts))
    return {
        "score": score,
        "confidence": round(confidence, 3),
        "reasons": reasons[:5],
        "metadata": meta,
        "available": True,
    }


def score_relative_strength_from_audit(
    audit: dict[str, Any],
    *,
    cohort_audits: list[dict[str, Any]] | None = None,
) -> FactorResult:
    """Build RS factor from audit fields (and optional cohort for percentile rank)."""
    horizons = (20, 50, 90)
    parts: list[float] = []
    reasons: list[str] = []
    meta: dict[str, Any] = {}

    for n in horizons:
        nk = f"rel_return_{n}d_vs_nifty_pct"
        val = _sf(audit.get(nk))
        if n == 50 and math.isnan(val):
            val = _sf(audit.get("rel_return_20d_vs_nifty_pct"))  # fallback proxy
        if not math.isnan(val):
            meta[nk] = round(val, 4)
            parts.append(_rel_return_to_score(val))
            reasons.append(f"nifty {n}d RS {val:+.1f}%")

    pctile = _sf(audit.get("sector_relative_strength_pctile"))
    if not math.isnan(pctile):
        meta["sector_relative_strength_pctile"] = round(pctile, 2)
        parts.append(_clamp(pctile))
        reasons.append(f"sector RS pctile {pctile:.0f}")

    if cohort_audits:
        rel_vals = [
            _sf(a.get("rel_return_20d_vs_nifty_pct"))
            for a in cohort_audits
            if isinstance(a, dict)
        ]
        rel_vals = [v for v in rel_vals if not math.isnan(v)]
        own = _sf(audit.get("rel_return_20d_vs_nifty_pct"))
        if rel_vals and not math.isnan(own):
            rank = percentile_rank_0_100(rel_vals, own)
            meta["cohort_rank_pctile"] = rank
            if math.isnan(pctile):
                parts.append(_clamp(rank))
                reasons.append(f"cohort RS rank {rank:.0f}")

    if not parts:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["relative strength fields missing"],
            "metadata": meta,
            "available": False,
        }

    score = round(sum(parts) / len(parts), 2)
    return {
        "score": score,
        "confidence": round(min(1.0, 0.5 + 0.1 * len(parts)), 3),
        "reasons": reasons[:5],
        "metadata": meta,
        "available": True,
    }
