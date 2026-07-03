"""Sector rotation factor — complements sector_priority rankings."""

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


def _sector_score(row: dict[str, Any]) -> float:
    for key in (
        "sector_relative_rank_score",
        "relative_rank_score",
        "avg_effective_intent_score",
        "avg_intent_score",
        "momentum_score",
    ):
        v = _sf(row.get(key))
        if not math.isnan(v):
            return v
    rel20 = _sf(row.get("avg_rel_return_20d_vs_nifty_pct"))
    if not math.isnan(rel20):
        return _clamp(50.0 + rel20 * 2.5)
    return float("nan")


def rank_sectors(sector_rollups: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank sectors by relative strength; return leading/lagging metadata."""
    scored: list[tuple[str, float, dict[str, Any]]] = []
    for row in sector_rollups:
        if not isinstance(row, dict):
            continue
        key = str(row.get("sector_key") or row.get("sector") or "").strip().lower()
        if not key:
            continue
        s = _sector_score(row)
        if math.isnan(s):
            continue
        scored.append((key, s, row))

    scored.sort(key=lambda x: x[1], reverse=True)
    n = len(scored)
    ranks: dict[str, dict[str, Any]] = {}
    for i, (key, s, row) in enumerate(scored):
        pctile = round(100.0 * (n - 1 - i) / max(1, n - 1), 2) if n > 1 else 50.0
        ranks[key] = {
            "rank": i + 1,
            "score": round(s, 2),
            "percentile": pctile,
            "row": row,
        }

    leading = [k for k, v in ranks.items() if v["rank"] <= max(1, n // 5)]
    lagging = [k for k, v in ranks.items() if v["rank"] > max(1, n - max(1, n // 5))]

    return {
        "n_sectors": n,
        "ranks": ranks,
        "leading_sectors": leading,
        "lagging_sectors": lagging,
        "top_sector": scored[0][0] if scored else None,
        "bottom_sector": scored[-1][0] if scored else None,
    }


def score_sector_rotation(
    sector_rollups: list[dict[str, Any]],
    target_sector: str,
) -> FactorResult:
    """Score how the target sector ranks in the rotation landscape."""
    ranking = rank_sectors(sector_rollups)
    target = str(target_sector or "").strip().lower()
    entry = (ranking.get("ranks") or {}).get(target)

    if not entry:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": [f"sector {target or '?'} not in rollups"],
            "metadata": {"ranking": ranking},
            "available": False,
        }

    pctile = float(entry["percentile"])
    rank = int(entry["rank"])
    n = int(ranking.get("n_sectors") or 0)
    score = _clamp(pctile)
    reasons = [f"sector rank {rank}/{n} (pctile {pctile:.0f})"]
    if target in ranking.get("leading_sectors", []):
        reasons.append("leading sector")
    elif target in ranking.get("lagging_sectors", []):
        reasons.append("lagging sector")

    return {
        "score": round(score, 2),
        "confidence": round(min(1.0, 0.5 + 0.05 * n), 3),
        "reasons": reasons,
        "metadata": {
            "rank": rank,
            "n_sectors": n,
            "percentile": pctile,
            "leading_sectors": ranking.get("leading_sectors"),
            "lagging_sectors": ranking.get("lagging_sectors"),
        },
        "available": True,
    }
