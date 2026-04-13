"""Pure calculation helpers: DataFrame/Series in, float/bool out."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def calculate_z_score(data: pd.Series | pd.DataFrame, window: int = 20) -> float:
    """Rolling Z-score of the last observation vs trailing window (close column if DataFrame)."""
    series = data["close"] if isinstance(data, pd.DataFrame) else data
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty or window < 2:
        return float("nan")
    win = min(window, len(s))
    tail = s.iloc[-win:]
    mu = float(tail.mean())
    sigma = float(tail.std(ddof=0))
    last = float(tail.iloc[-1])
    if sigma == 0.0 or math.isnan(sigma):
        return 0.0
    return (last - mu) / sigma


def calculate_ema(data: pd.Series | pd.DataFrame, span: int = 200) -> float:
    """Last EMA value from close series (NaN when input is empty)."""
    series = data["close"] if isinstance(data, pd.DataFrame) else data
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty or span < 1:
        return float("nan")
    ema = s.ewm(span=span, adjust=False).mean()
    return float(ema.iloc[-1])


def calculate_atr(data: pd.DataFrame, window: int = 14) -> float:
    """
    Last ATR value from OHLC frame.
    Requires columns: high, low, close.
    """
    if data.empty or window < 1:
        return float("nan")
    req = {"high", "low", "close"}
    if not req.issubset(set(data.columns)):
        return float("nan")
    h = pd.to_numeric(data["high"], errors="coerce")
    l = pd.to_numeric(data["low"], errors="coerce")
    c = pd.to_numeric(data["close"], errors="coerce")
    prev_c = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    tr = tr.dropna()
    if tr.empty:
        return float("nan")
    atr = tr.rolling(window=min(window, len(tr)), min_periods=1).mean()
    return float(atr.iloc[-1])


def calculate_absorption_ratio(current_delivery: float, avg_delivery_5d: float) -> float:
    """Current delivery vs 5d average; safe when average is zero or invalid."""
    if avg_delivery_5d is None or (isinstance(avg_delivery_5d, float) and math.isnan(avg_delivery_5d)):
        return float("nan")
    if avg_delivery_5d == 0.0:
        return float("inf") if current_delivery and current_delivery > 0 else 0.0
    return float(current_delivery) / float(avg_delivery_5d)


def get_pcr(total_put_oi: float, total_call_oi: float) -> float:
    """Put-call ratio: put OI / call OI."""
    if total_call_oi == 0.0:
        return float("inf") if total_put_oi > 0 else float("nan")
    return float(total_put_oi) / float(total_call_oi)


def find_oi_walls(option_chain_df: pd.DataFrame) -> dict[str, Any]:
    """Return strike with maximum open interest (ignores NaN OI)."""
    df = option_chain_df.copy()
    if "oi" not in df.columns or "strike" not in df.columns:
        raise ValueError("option_chain_df must contain 'strike' and 'oi' columns")
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
    df = df.dropna(subset=["oi", "strike"])
    if df.empty:
        return {"strike": float("nan"), "oi": float("nan")}
    idx = df["oi"].idxmax()
    row = df.loc[idx]
    return {"strike": float(row["strike"]), "oi": float(row["oi"])}


def calculate_intent_score(pcr: float, z_score: float, absorption: float) -> float:
    """Map technicals to 0-100 via weighted normalized blend."""
    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, x))

    def norm_pcr(p: float) -> float:
        if math.isnan(p) or math.isinf(p):
            return 0.5
        t = math.atan(p) / (math.pi / 2)
        return clamp01(0.5 + 0.5 * t)

    def norm_z(z: float) -> float:
        if math.isnan(z):
            return 0.5
        return clamp01(0.5 + 0.5 * math.tanh(z / 3.0))

    def norm_abs(a: float) -> float:
        if math.isnan(a):
            return 0.5
        if math.isinf(a) and a > 0:
            return 1.0
        return clamp01(a / 3.0)

    w_pcr, w_z, w_a = 0.35, 0.35, 0.30
    blended = w_pcr * norm_pcr(pcr) + w_z * norm_z(z_score) + w_a * norm_abs(absorption)
    return round(100.0 * blended, 2)
