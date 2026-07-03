"""Market breadth metrics and macro context stamping for sector runs."""

from __future__ import annotations

import math
import os
from typing import Any, Sequence

import pandas as pd

from score_types import FactorResult


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _panel_closes(panel: dict[str, pd.DataFrame] | list[dict[str, Any]]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    if isinstance(panel, list):
        for row in panel:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            closes = row.get("closes")
            if sym and closes is not None:
                out[sym] = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
        return out
    for sym, df in panel.items():
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            continue
        if isinstance(df, pd.DataFrame):
            col = "close" if "close" in df.columns else df.columns[-1]
            s = pd.to_numeric(df[col], errors="coerce").dropna()
        else:
            s = pd.to_numeric(pd.Series(df), errors="coerce").dropna()
        if not s.empty:
            out[str(sym).upper()] = s
    return out


def _pct_above_ema(closes: pd.Series, span: int) -> float:
    s = closes.dropna()
    if len(s) < span + 1:
        return float("nan")
    ema = s.ewm(span=span, adjust=False).mean()
    last_close = float(s.iloc[-1])
    last_ema = float(ema.iloc[-1])
    if math.isnan(last_close) or math.isnan(last_ema):
        return float("nan")
    return 100.0 if last_close > last_ema else 0.0


def compute_market_breadth(
    panel: dict[str, pd.DataFrame] | list[dict[str, Any]],
    *,
    lookback_high_low: int = 252,
) -> dict[str, Any]:
    """
    Cross-section breadth: advance/decline ratio, % above EMA20/50/200, new highs/lows.
    """
    series_map = _panel_closes(panel)
    n = len(series_map)
    if n == 0:
        return {
            "n_symbols": 0,
            "advance_decline_ratio": None,
            "pct_above_ema20": None,
            "pct_above_ema50": None,
            "pct_above_ema200": None,
            "new_highs_pct": None,
            "new_lows_pct": None,
        }

    advances = 0
    declines = 0
    above20 = above50 = above200 = 0
    new_highs = new_lows = 0
    counted20 = counted50 = counted200 = 0
    counted_hl = 0

    for s in series_map.values():
        if len(s) < 2:
            continue
        ret1 = (float(s.iloc[-1]) / float(s.iloc[-2]) - 1.0) if float(s.iloc[-2]) != 0 else 0.0
        if ret1 > 0:
            advances += 1
        elif ret1 < 0:
            declines += 1

        for span, bucket in ((20, "20"), (50, "50"), (200, "200")):
            flag = _pct_above_ema(s, span)
            if math.isnan(flag):
                continue
            if bucket == "20":
                counted20 += 1
                above20 += int(flag > 0)
            elif bucket == "50":
                counted50 += 1
                above50 += int(flag > 0)
            else:
                counted200 += 1
                above200 += int(flag > 0)

        lb = min(lookback_high_low, len(s) - 1)
        if lb >= 20:
            window = s.iloc[-(lb + 1) : -1]
            last = float(s.iloc[-1])
            if not window.empty and not math.isnan(last):
                counted_hl += 1
                if last >= float(window.max()):
                    new_highs += 1
                if last <= float(window.min()):
                    new_lows += 1

    ad_ratio: float | None
    if declines > 0:
        ad_ratio = round(advances / declines, 4)
    elif advances > 0:
        ad_ratio = float("inf")
    else:
        ad_ratio = None

    def _pct(count: int, denom: int) -> float | None:
        return round(100.0 * count / denom, 2) if denom > 0 else None

    return {
        "n_symbols": n,
        "advance_decline_ratio": ad_ratio,
        "pct_above_ema20": _pct(above20, counted20),
        "pct_above_ema50": _pct(above50, counted50),
        "pct_above_ema200": _pct(above200, counted200),
        "new_highs_pct": _pct(new_highs, counted_hl),
        "new_lows_pct": _pct(new_lows, counted_hl),
    }


def score_breadth(metrics: dict[str, Any]) -> FactorResult:
    """Map breadth metrics to a 0–100 diagnostic score (not a fusion pillar)."""
    if not metrics or int(metrics.get("n_symbols") or 0) < 2:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["breadth panel too small"],
            "metadata": dict(metrics or {}),
            "available": False,
        }

    parts: list[float] = []
    reasons: list[str] = []

    ema200 = metrics.get("pct_above_ema200")
    if ema200 is not None:
        v = _sf(ema200)
        if not math.isnan(v):
            parts.append(_clamp(v))
            reasons.append(f"{v:.0f}% above EMA200")

    ema50 = metrics.get("pct_above_ema50")
    if ema50 is not None:
        v = _sf(ema50)
        if not math.isnan(v):
            parts.append(_clamp(v))
            reasons.append(f"{v:.0f}% above EMA50")

    ad = metrics.get("advance_decline_ratio")
    if ad is not None and ad != float("inf"):
        v = _sf(ad)
        if not math.isnan(v):
            ad_score = _clamp(50.0 + (v - 1.0) * 25.0)
            parts.append(ad_score)
            reasons.append(f"A/D ratio {v:.2f}")

    nh = _sf(metrics.get("new_highs_pct"))
    nl = _sf(metrics.get("new_lows_pct"))
    if not math.isnan(nh) and not math.isnan(nl):
        hl_score = _clamp(50.0 + (nh - nl) * 0.5)
        parts.append(hl_score)
        reasons.append(f"new highs {nh:.0f}% / lows {nl:.0f}%")

    if not parts:
        return {
            "score": None,
            "confidence": 0.0,
            "reasons": ["breadth metrics incomplete"],
            "metadata": dict(metrics),
            "available": False,
        }

    score = round(sum(parts) / len(parts), 2)
    return {
        "score": score,
        "confidence": round(min(1.0, 0.55 + 0.08 * len(parts)), 3),
        "reasons": reasons[:5],
        "metadata": {**metrics, "diagnostic": True},
        "available": True,
    }


def stamp_macro_context(
    *,
    nifty_df: pd.DataFrame | None = None,
    macro_snapshot: dict[str, Any] | None = None,
    breadth_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Stamp NIFTY EMA50/200, VIX, and breadth once per sector run.
    Returns a context dict to merge onto each audit.
    """
    ctx: dict[str, Any] = {}
    if nifty_df is not None and not nifty_df.empty:
        col = "close" if "close" in nifty_df.columns else nifty_df.columns[-1]
        closes = pd.to_numeric(nifty_df[col], errors="coerce").dropna()
        if not closes.empty:
            last = float(closes.iloc[-1])
            ctx["nifty_close"] = round(last, 2)
            for span, key in ((50, "nifty_ema50"), (200, "nifty_ema200")):
                if len(closes) >= span:
                    ema = float(closes.ewm(span=span, adjust=False).mean().iloc[-1])
                    ctx[key] = round(ema, 2)
                    if last > 0 and not math.isnan(ema):
                        dist_key = "nifty_ema200_distance_pct" if span == 200 else f"nifty_ema{span}_distance_pct"
                        ctx[dist_key] = round(((last / ema) - 1.0) * 100.0, 4)
            ema200 = ctx.get("nifty_ema200")
            if ema200 is not None:
                ctx["nifty_above_ema200"] = bool(last > float(ema200))

    if isinstance(macro_snapshot, dict):
        vix = _sf(macro_snapshot.get("india_vix", macro_snapshot.get("vix")))
        if not math.isnan(vix):
            ctx["india_vix"] = round(vix, 2)
        gift = _sf(macro_snapshot.get("gift_nifty_change_pct"))
        if not math.isnan(gift):
            ctx["gift_nifty_change_pct"] = round(gift, 4)

    if isinstance(breadth_metrics, dict) and breadth_metrics:
        ctx["market_breadth"] = dict(breadth_metrics)
        ema200_pct = breadth_metrics.get("pct_above_ema200")
        if ema200_pct is not None:
            ctx["market_breadth_pct"] = ema200_pct
            ctx["breadth_above_ema200_pct"] = ema200_pct
        breadth_score = score_breadth(breadth_metrics)
        ctx["breadth_factor"] = breadth_score

    return ctx


def prefetch_breadth_panel(
    cfg: Any,
    breeze: Any,
    instruments: Sequence[Any],
    *,
    lookback_calendar_days: int = 280,
    max_symbols: int | None = None,
) -> dict[str, pd.DataFrame]:
    """Prefetch OHLC panel for breadth computation (best-effort per symbol).

    ``max_symbols`` caps panel size for latency control. In sector runs this is
    typically set from ``TITAN_BREADTH_PANEL_MAX_SYMBOLS`` (unset = full universe).
    Breadth is diagnostic only in fusion — it does not enter ``titan_score`` weights.
    """
    from breeze_client import fetch_equity_data

    panel: dict[str, pd.DataFrame] = {}
    subset = list(instruments)
    if max_symbols is not None:
        subset = subset[: max(0, int(max_symbols))]
    for inst in subset:
        symbol = str(getattr(inst, "symbol", inst) or "").strip().upper()
        exchange = str(getattr(inst, "exchange", "NSE") or "NSE").strip().upper()
        if not symbol:
            continue
        try:
            df = fetch_equity_data(
                cfg,
                symbol,
                exchange,
                breeze=breeze,
                lookback_calendar_days=lookback_calendar_days,
            )
            if df is not None and not df.empty:
                panel[symbol] = df
        except Exception:
            continue
    return panel


def apply_macro_context_to_audit(audit: dict[str, Any], ctx: dict[str, Any]) -> None:
    """Merge stamped macro/breadth context onto a symbol audit (non-destructive)."""
    if not ctx:
        return
    for key, value in ctx.items():
        if key == "breadth_factor":
            audit["breadth"] = value
            if isinstance(value, dict) and value.get("score") is not None:
                audit["breadth_score"] = value.get("score")
        elif key == "market_breadth":
            audit["market_breadth"] = value
        elif audit.get(key) is None:
            audit[key] = value
