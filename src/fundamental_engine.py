"""Fundamental quality factor — migrated from sector_audit._assess_fundamental_strength."""

from __future__ import annotations

import math
import threading
from typing import Any

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig
from score_types import FactorResult
from sector_registry import SectorInstrument

_CACHE_LOCK = threading.Lock()
_FUNDAMENTAL_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp_score(x: float) -> float:
    return max(0.0, min(100.0, round(x, 2)))


def _first_float_field(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        if k in row:
            v = _sf(row.get(k))
            if not math.isnan(v):
                return v
    return float("nan")


def _fmt_metric(x: Any, digits: int = 2) -> str:
    v = _sf(x)
    if math.isnan(v):
        return "n/a"
    return f"{v:.{digits}f}"


def score_fundamentals(row: dict[str, Any]) -> FactorResult:
    """
    Score fundamental quality from a market_instruments row.
    Gracefully skips unavailable metrics.
    """
    roe = _first_float_field(row, ("roe", "roe_pct", "return_on_equity", "return_on_equity_pct"))
    roce = _first_float_field(row, ("roce", "roce_pct", "return_on_capital", "return_on_capital_employed"))
    de = _first_float_field(row, ("debt_to_equity", "de_ratio", "debt_equity"))
    margin = _first_float_field(row, ("net_profit_margin", "npm", "operating_margin", "opm"))
    rev_growth = _first_float_field(row, ("revenue_growth_pct", "revenue_growth", "sales_growth_pct"))
    eps_growth = _first_float_field(row, ("eps_growth_pct", "earnings_growth_pct"))
    pe = _first_float_field(row, ("pe", "pe_ratio", "trailing_pe"))
    pb = _first_float_field(row, ("pb", "pb_ratio", "price_to_book"))

    score = 50.0
    reasons: list[str] = []
    used = 0
    quality_hits = 0
    growth_hits = 0
    valuation_hits = 0
    health_hits = 0
    meta: dict[str, Any] = {}

    if not math.isnan(roe):
        used += 1
        meta["roe"] = round(roe, 2)
        if roe >= 15.0:
            score += 12.0
            quality_hits += 1
            reasons.append(f"ROE strong {_fmt_metric(roe)}")
        elif roe >= 10.0:
            score += 6.0
            quality_hits += 1
            reasons.append(f"ROE acceptable {_fmt_metric(roe)}")
        elif roe < 5.0:
            score -= 8.0
            reasons.append(f"ROE weak {_fmt_metric(roe)}")
    if not math.isnan(roce):
        used += 1
        meta["roce"] = round(roce, 2)
        if roce >= 15.0:
            score += 10.0
            quality_hits += 1
            reasons.append(f"ROCE strong {_fmt_metric(roce)}")
        elif roce >= 10.0:
            score += 5.0
            quality_hits += 1
            reasons.append(f"ROCE acceptable {_fmt_metric(roce)}")
        elif roce < 6.0:
            score -= 6.0
            reasons.append(f"ROCE weak {_fmt_metric(roce)}")
    if not math.isnan(margin):
        used += 1
        meta["margin"] = round(margin, 2)
        if margin >= 12.0:
            score += 6.0
            quality_hits += 1
            reasons.append(f"margin strong {_fmt_metric(margin)}")
        elif margin < 3.0:
            score -= 4.0
            reasons.append(f"margin thin {_fmt_metric(margin)}")
    if not math.isnan(de):
        used += 1
        meta["debt_to_equity"] = round(de, 2)
        if de <= 0.5:
            score += 8.0
            health_hits += 1
            reasons.append(f"debt/equity low {_fmt_metric(de)}")
        elif de <= 1.0:
            score += 4.0
            health_hits += 1
            reasons.append(f"debt/equity moderate {_fmt_metric(de)}")
        elif de > 2.0:
            score -= 8.0
            reasons.append(f"debt/equity high {_fmt_metric(de)}")
    if not math.isnan(rev_growth):
        used += 1
        meta["revenue_growth_pct"] = round(rev_growth, 2)
        if rev_growth >= 15.0:
            score += 6.0
            growth_hits += 1
            reasons.append(f"revenue growth strong {_fmt_metric(rev_growth)}")
        elif rev_growth < 0.0:
            score -= 4.0
            reasons.append(f"revenue contraction {_fmt_metric(rev_growth)}")
    if not math.isnan(eps_growth):
        used += 1
        meta["eps_growth_pct"] = round(eps_growth, 2)
        if eps_growth >= 10.0:
            score += 4.0
            growth_hits += 1
    if not math.isnan(pe):
        used += 1
        meta["pe"] = round(pe, 2)
        if 5.0 <= pe <= 25.0:
            score += 3.0
            valuation_hits += 1
        elif pe > 40.0:
            score -= 4.0
            reasons.append(f"PE elevated {_fmt_metric(pe)}")
    if not math.isnan(pb):
        meta["pb"] = round(pb, 2)

    meta["quality_signals"] = quality_hits
    meta["growth_signals"] = growth_hits
    meta["valuation_signals"] = valuation_hits
    meta["health_signals"] = health_hits

    if used == 0:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["fundamental fields unavailable"],
            "metadata": meta,
            "available": False,
        }

    s = _clamp_score(score)
    if s >= 65.0:
        status = "strong"
    elif s <= 40.0:
        status = "weak"
    else:
        status = "balanced"
    meta["status"] = status

    return {
        "score": s,
        "confidence": round(min(1.0, 0.4 + 0.12 * used), 3),
        "reasons": reasons[:5] if reasons else [f"fundamental status {status}"],
        "metadata": meta,
        "available": True,
    }


def assess_fundamental_strength(cfg: TitanConfig, inst: SectorInstrument) -> dict[str, Any]:
    """DB-backed fundamental assessment with in-process cache (sector_audit wrapper target)."""
    cache_key = (inst.symbol, inst.exchange)
    with _CACHE_LOCK:
        cached = _FUNDAMENTAL_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("market_instruments")
            .select("*")
            .eq("symbol", inst.symbol)
            .eq("exchange", inst.exchange)
            .limit(1)
            .execute()
        )
    except (APIError, Exception):
        out = {"status": "unavailable", "score": None, "reasons": ["fundamental lookup unavailable"]}
        with _CACHE_LOCK:
            _FUNDAMENTAL_CACHE[cache_key] = dict(out)
        return out

    rows = list(getattr(res, "data", None) or [])
    if not rows or not isinstance(rows[0], dict):
        out = {"status": "unavailable", "score": None, "reasons": ["fundamental row missing"]}
        with _CACHE_LOCK:
            _FUNDAMENTAL_CACHE[cache_key] = dict(out)
        return out

    factor = score_fundamentals(rows[0])
    if not factor.get("available"):
        out = {
            "status": "unavailable",
            "score": None,
            "reasons": list(factor.get("reasons") or []),
            "factor": factor,
        }
    else:
        meta = factor.get("metadata") or {}
        out = {
            "status": meta.get("status", "balanced"),
            "score": factor.get("score"),
            "reasons": list(factor.get("reasons") or [])[:3],
            "factor": factor,
        }

    with _CACHE_LOCK:
        _FUNDAMENTAL_CACHE[cache_key] = dict(out)
    return out
