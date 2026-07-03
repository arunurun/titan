"""Institutional flow factor — reads CMF/OBV from audit; never recomputes them."""

from __future__ import annotations

import logging
import math
from typing import Any

from score_types import FactorResult

logger = logging.getLogger(__name__)


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        if key in row:
            v = _sf(row.get(key))
            if not math.isnan(v):
                return v
    return float("nan")


def _deal_side_score(items: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """Net buy-side block/bulk deal pressure → 0–100 sub-score."""
    if not items:
        return None, None
    buy_qty = 0.0
    sell_qty = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        side = str(item.get("side") or item.get("buy_sell") or "").strip().upper()
        qty = _sf(item.get("qty") or item.get("quantity"))
        if math.isnan(qty):
            continue
        if side.startswith("B"):
            buy_qty += qty
        elif side.startswith("S"):
            sell_qty += qty
    total = buy_qty + sell_qty
    if total <= 0:
        return None, None
    net = (buy_qty - sell_qty) / total
    score = _clamp(50.0 + net * 35.0)
    note = f"deals net {'buy' if net > 0 else 'sell' if net < 0 else 'flat'}"
    return score, note


def enrich_audit_institutional_data(
    audit: dict[str, Any],
    cfg: Any,
    inst: Any,
    *,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """
    Populate audit institutional fields from NSE delivery, deals, and Supabase rows.
    Gracefully skips missing sources (``available=False`` when nothing found).
    """
    symbol = str(getattr(inst, "symbol", inst) or audit.get("symbol") or "").strip().upper()
    exchange = str(getattr(inst, "exchange", audit.get("exchange")) or "NSE").strip().upper()
    meta: dict[str, Any] = {"symbol": symbol, "exchange": exchange, "sources": []}
    available = False

    try:
        from breakout_eod_context import load_delivery_pct_by_symbol

        delivery_map = load_delivery_pct_by_symbol([symbol], as_of_date=as_of_date)
        delivery = delivery_map.get(symbol)
        if delivery is not None:
            audit["delivery_pct"] = round(float(delivery), 2)
            meta["delivery_pct"] = audit["delivery_pct"]
            meta["sources"].append("delivery_daily")
            available = True
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("delivery_pct enrich skipped for %s: %s", symbol, exc)

    try:
        from sector_priority import fetch_nse_bulk_block_deals

        deals = fetch_nse_bulk_block_deals(symbol)
        items = list(deals.get("items") or [])
        if items:
            audit["nse_block_bulk_deals"] = items
            meta["nse_deals_count"] = len(items)
            meta["sources"].append("nse_bulk_block_deals")
            available = True
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("bulk/block deals enrich skipped for %s: %s", symbol, exc)

    inst_row: dict[str, Any] | None = None
    if cfg is not None and symbol:
        try:
            from postgrest.exceptions import APIError
            from supabase import create_client

            client = create_client(cfg.supabase_url, cfg.supabase_key)
            res = (
                client.table("market_instruments")
                .select("*")
                .eq("symbol", symbol)
                .eq("exchange", exchange)
                .limit(1)
                .execute()
            )
            rows = list(getattr(res, "data", None) or [])
            if rows and isinstance(rows[0], dict):
                inst_row = rows[0]
        except (ImportError, APIError, Exception) as exc:  # noqa: BLE001
            logger.debug("market_instruments enrich skipped for %s: %s", symbol, exc)

    if inst_row:
        for field, keys in (
            ("fii_holding_pct", ("fii_holding_pct", "fii_holding", "fii_stake_pct")),
            ("dii_holding_pct", ("dii_holding_pct", "dii_holding", "dii_stake_pct")),
            ("promoter_holding_pct", ("promoter_holding_pct", "promoter_holding", "promoter_stake_pct")),
            ("mf_holding_pct", ("mf_holding_pct", "mf_holding", "mutual_fund_holding_pct")),
            ("fii_holding_change_pct", ("fii_holding_change_pct", "fii_holding_chg_pct")),
            ("promoter_holding_change_pct", ("promoter_holding_change_pct", "promoter_holding_chg_pct")),
            ("mf_holding_change_pct", ("mf_holding_change_pct", "mf_holding_chg_pct")),
        ):
            val = _first_float(inst_row, keys)
            if not math.isnan(val):
                audit[field] = round(val, 4)
                meta[field] = audit[field]
                meta["sources"].append("market_instruments")
                available = True

    macro = audit.get("market_institutional_flow")
    if isinstance(macro, dict):
        for key in ("fii_net_crs", "dii_net_crs"):
            v = _sf(macro.get(key))
            if not math.isnan(v):
                audit[key] = round(v, 2)
                meta[key] = audit[key]
                available = True

    flow_obj = {
        "available": available,
        "source": "+".join(meta["sources"]) if meta["sources"] else None,
        "metadata": meta,
    }
    audit["institutional_flow"] = flow_obj
    return flow_obj


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

    deals = audit.get("nse_block_bulk_deals")
    if isinstance(deals, list) and deals:
        deal_score, deal_note = _deal_side_score(deals)
        if deal_score is not None:
            meta["nse_deals_count"] = len(deals)
            parts.append(deal_score)
            weights.append(0.12)
            if deal_note:
                reasons.append(deal_note)

    fii_chg = _sf(audit.get("fii_holding_change_pct"))
    if not math.isnan(fii_chg):
        meta["fii_holding_change_pct"] = round(fii_chg, 2)
        parts.append(_clamp(50.0 + fii_chg * 2.5))
        weights.append(0.08)
        reasons.append(f"FII holding chg {fii_chg:+.1f}pp")

    prom_chg = _sf(audit.get("promoter_holding_change_pct"))
    if not math.isnan(prom_chg):
        meta["promoter_holding_change_pct"] = round(prom_chg, 2)
        parts.append(_clamp(50.0 + prom_chg * 1.5))
        weights.append(0.06)
        reasons.append(f"promoter holding chg {prom_chg:+.1f}pp")

    mf_chg = _sf(audit.get("mf_holding_change_pct"))
    if not math.isnan(mf_chg):
        meta["mf_holding_change_pct"] = round(mf_chg, 2)
        parts.append(_clamp(50.0 + mf_chg * 2.0))
        weights.append(0.06)
        reasons.append(f"MF holding chg {mf_chg:+.1f}pp")

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
