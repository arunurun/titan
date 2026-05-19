"""Shared tape / cross-section helpers for equity sector audits."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _safe_float(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v if not math.isnan(v) else float("nan")


def percentile_rank_0_100(values: list[float], x: float) -> float:
    """
    Empirical percentile of x in values (0 = lowest, 100 = highest).
    Ties: mid-rank. Returns nan if values empty or x is nan.
    """
    if math.isnan(x):
        return float("nan")
    xs = sorted(v for v in values if not math.isnan(v))
    if not xs:
        return float("nan")
    below = sum(1 for v in xs if v < x)
    equal = sum(1 for v in xs if v == x)
    # Mid-rank for ties
    rank = below + (equal - 1) / 2.0 if equal else below
    return round(100.0 * rank / max(1, len(xs) - 1) if len(xs) > 1 else 50.0, 2)


def pct_return_n_sessions_back(closes: pd.Series, n: int) -> float:
    """Close-to-close % return over the last n sessions (n>=1)."""
    s = pd.to_numeric(closes, errors="coerce").dropna()
    if len(s) < n + 1 or n < 1:
        return float("nan")
    last = float(s.iloc[-1])
    base = float(s.iloc[-(n + 1)])
    if base == 0.0 or math.isnan(last) or math.isnan(base):
        return float("nan")
    return round(((last / base) - 1.0) * 100.0, 4)


def median_notional_inr_20d(df: pd.DataFrame, close_col: str) -> float:
    """Median daily notional (close * volume) over last 20 rows, INR-ish if volume is shares."""
    if df.empty or close_col not in df.columns or "volume" not in df.columns:
        return float("nan")
    tail = df.tail(20)
    c = pd.to_numeric(tail[close_col], errors="coerce")
    v = pd.to_numeric(tail["volume"], errors="coerce")
    notion = (c * v).dropna()
    if notion.empty:
        return float("nan")
    return float(notion.median())


def _standardize_dates(df: pd.DataFrame) -> pd.Series | None:
    for col in ("datetime", "date", "Date", "time"):
        if col in df.columns:
            return pd.to_datetime(df[col], errors="coerce").dt.normalize()
    return None


def benchmark_relative_returns(
    stock_df: pd.DataFrame,
    bench_df: pd.DataFrame | None,
    close_col_stock: str,
    horizons: tuple[int, ...] = (5, 10, 20),
) -> dict[str, float]:
    """
    Stock minus benchmark close-to-close % over the same calendar rows (inner join on date).

    Returns keys rel_return_{n}d_vs_nifty_pct for each horizon, or nan if unavailable.
    """
    out: dict[str, float] = {f"rel_return_{n}d_vs_nifty_pct": float("nan") for n in horizons}
    if bench_df is None or bench_df.empty or stock_df.empty:
        return out
    d_s = _standardize_dates(stock_df)
    close_b_col = "close" if "close" in bench_df.columns else bench_df.columns[-1]
    d_b = _standardize_dates(bench_df)
    if d_s is None or d_b is None:
        return out
    s = stock_df[[close_col_stock]].copy()
    s["d"] = d_s.values
    s["sc"] = pd.to_numeric(s[close_col_stock], errors="coerce")
    s = s.dropna(subset=["d", "sc"])[["d", "sc"]]

    b = bench_df[[close_b_col]].copy()
    b["d"] = d_b.values
    b["bc"] = pd.to_numeric(b[close_b_col], errors="coerce")
    b = b.dropna(subset=["d", "bc"])[["d", "bc"]]

    m = pd.merge(s, b, on="d", how="inner").sort_values("d").reset_index(drop=True)
    if len(m) < max(horizons) + 2:
        return out
    for n in horizons:
        if len(m) < n + 1:
            continue
        sl = float(m["sc"].iloc[-1])
        sb = float(m["sc"].iloc[-(n + 1)])
        bl = float(m["bc"].iloc[-1])
        bb = float(m["bc"].iloc[-(n + 1)])
        if any(math.isnan(x) or x == 0 for x in (sl, sb, bl, bb)):
            continue
        rs = (sl / sb - 1.0) * 100.0
        rb = (bl / bb - 1.0) * 100.0
        out[f"rel_return_{n}d_vs_nifty_pct"] = round(rs - rb, 4)
    return out
