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


def calculate_rsi(data: pd.Series | pd.DataFrame, period: int = 14) -> float:
    """Last RSI value from close series (Wilder smoothing; NaN when insufficient history)."""
    series = data["close"] if isinstance(data, pd.DataFrame) else data
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < period + 1 or period < 1:
        return float("nan")
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0.0 or math.isnan(last_loss):
        return 100.0 if float(avg_gain.iloc[-1]) > 0.0 else 50.0
    rs = float(avg_gain.iloc[-1]) / last_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    return float(out) if not math.isnan(out) else float("nan")


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


def calculate_adx(data: pd.DataFrame, window: int = 14) -> float:
    """
    Last ADX value from OHLC frame.
    Requires columns: high, low, close.
    """
    if data.empty or window < 2:
        return float("nan")
    req = {"high", "low", "close"}
    if not req.issubset(set(data.columns)):
        return float("nan")

    h = pd.to_numeric(data["high"], errors="coerce")
    l = pd.to_numeric(data["low"], errors="coerce")
    c = pd.to_numeric(data["close"], errors="coerce")
    prev_h = h.shift(1)
    prev_l = l.shift(1)
    prev_c = c.shift(1)

    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    plus_dm = (h - prev_h).where((h - prev_h) > (prev_l - l), 0.0).clip(lower=0.0)
    minus_dm = (prev_l - l).where((prev_l - l) > (h - prev_h), 0.0).clip(lower=0.0)

    roll = min(window, len(data))
    tr_n = tr.rolling(window=roll, min_periods=1).sum()
    plus_n = plus_dm.rolling(window=roll, min_periods=1).sum()
    minus_n = minus_dm.rolling(window=roll, min_periods=1).sum()
    if tr_n.empty:
        return float("nan")

    plus_di = 100.0 * (plus_n / tr_n.replace(0.0, np.nan))
    minus_di = 100.0 * (minus_n / tr_n.replace(0.0, np.nan))
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = ((plus_di - minus_di).abs() / denom) * 100.0
    dx = dx.dropna()
    if dx.empty:
        return float("nan")

    adx = dx.rolling(window=min(window, len(dx)), min_periods=1).mean()
    out = float(adx.iloc[-1])
    return out if not math.isnan(out) else float("nan")


def calculate_latest_di(data: pd.DataFrame, window: int = 14) -> tuple[float, float]:
    """
    Returns latest (+DI, -DI) for the chosen window.
    """
    if data.empty:
        return float("nan"), float("nan")
    req = {"high", "low", "close"}
    if not req.issubset(set(data.columns)):
        return float("nan"), float("nan")

    h = pd.to_numeric(data["high"], errors="coerce")
    l = pd.to_numeric(data["low"], errors="coerce")
    c = pd.to_numeric(data["close"], errors="coerce")
    prev_h = h.shift(1)
    prev_l = l.shift(1)
    prev_c = c.shift(1)

    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    plus_dm = (h - prev_h).where((h - prev_h) > (prev_l - l), 0.0).clip(lower=0.0)
    minus_dm = (prev_l - l).where((prev_l - l) > (h - prev_h), 0.0).clip(lower=0.0)

    roll = min(window, len(data))
    tr_n = tr.rolling(window=roll, min_periods=1).sum()
    plus_n = plus_dm.rolling(window=roll, min_periods=1).sum()
    minus_n = minus_dm.rolling(window=roll, min_periods=1).sum()
    if tr_n.empty:
        return float("nan"), float("nan")

    plus_di = 100.0 * (plus_n / tr_n.replace(0.0, np.nan))
    minus_di = 100.0 * (minus_n / tr_n.replace(0.0, np.nan))
    plus_series = plus_di.dropna()
    minus_series = minus_di.dropna()
    plus_out = float(plus_series.iloc[-1]) if not plus_series.empty else float("nan")
    minus_out = float(minus_series.iloc[-1]) if not minus_series.empty else float("nan")
    return plus_out, minus_out


def calculate_breakout_20d_distances_pct(data: pd.DataFrame) -> tuple[float, float]:
    """
    Returns (pct_to_20d_high, pct_above_20d_low) for latest close.
    """
    if data.empty or "close" not in data.columns:
        return float("nan"), float("nan")
    closes = pd.to_numeric(data["close"], errors="coerce").dropna()
    if closes.empty:
        return float("nan"), float("nan")
    win = min(20, len(closes))
    tail = closes.iloc[-win:]
    high_20 = float(tail.max()) if not tail.empty else float("nan")
    low_20 = float(tail.min()) if not tail.empty else float("nan")
    last = float(tail.iloc[-1]) if not tail.empty else float("nan")

    pct_to_high = (
        ((last / high_20) - 1.0) * 100.0
        if not math.isnan(last) and not math.isnan(high_20) and high_20 != 0.0
        else float("nan")
    )
    pct_above_low = (
        ((last / low_20) - 1.0) * 100.0
        if not math.isnan(last) and not math.isnan(low_20) and low_20 != 0.0
        else float("nan")
    )
    return pct_to_high, pct_above_low


def calculate_atr_ratio(data: pd.DataFrame, short_window: int = 14, long_window: int = 63) -> float:
    """ATR short/long ratio (NaN-safe)."""
    atr_short = calculate_atr(data, window=short_window)
    atr_long = calculate_atr(data, window=long_window)
    if math.isnan(atr_short) or math.isnan(atr_long) or atr_long == 0.0:
        return float("nan")
    return float(atr_short / atr_long)


def calculate_cmf(data: pd.DataFrame, window: int = 20) -> float:
    """
    Chaikin Money Flow (CMF) over the latest rolling window.
    Requires columns: high, low, close, volume.
    """
    if data.empty or window < 1:
        return float("nan")
    req = {"high", "low", "close", "volume"}
    if not req.issubset(set(data.columns)):
        return float("nan")

    h = pd.to_numeric(data["high"], errors="coerce")
    l = pd.to_numeric(data["low"], errors="coerce")
    c = pd.to_numeric(data["close"], errors="coerce")
    v = pd.to_numeric(data["volume"], errors="coerce")
    hl_range = (h - l).replace(0.0, np.nan)
    mfm = (((c - l) - (h - c)) / hl_range).replace([np.inf, -np.inf], np.nan)
    mfv = mfm * v
    roll = min(window, len(data))
    mfv_sum = mfv.rolling(window=roll, min_periods=1).sum()
    vol_sum = v.rolling(window=roll, min_periods=1).sum().replace(0.0, np.nan)
    cmf = (mfv_sum / vol_sum).replace([np.inf, -np.inf], np.nan).dropna()
    if cmf.empty:
        return float("nan")
    return float(cmf.iloc[-1])


def _obv_series(data: pd.DataFrame) -> pd.Series:
    """Cumulative on-balance volume; empty when close/volume unavailable."""
    if data.empty:
        return pd.Series(dtype=float)
    req = {"close", "volume"}
    if not req.issubset(set(data.columns)):
        return pd.Series(dtype=float)
    c = pd.to_numeric(data["close"], errors="coerce")
    v = pd.to_numeric(data["volume"], errors="coerce")
    if c.dropna().empty or v.dropna().empty:
        return pd.Series(dtype=float)
    delta = c.diff()
    direction = delta.apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))
    return (direction * v.fillna(0.0)).cumsum().dropna()


def calculate_obv_latest(data: pd.DataFrame) -> float:
    """Latest cumulative OBV level (signed, not normalized)."""
    obv = _obv_series(data)
    if obv.empty:
        return float("nan")
    return float(obv.iloc[-1])


def calculate_obv_ema(data: pd.DataFrame, span: int = 20) -> float:
    """20-session EMA of the OBV series (trend baseline)."""
    obv = _obv_series(data)
    if obv.empty or span < 1:
        return float("nan")
    ema = obv.ewm(span=span, adjust=False).mean()
    return float(ema.iloc[-1])


def calculate_obv_trend_confirm(data: pd.DataFrame, span: int = 20) -> bool | None:
    """True when current OBV exceeds its EMA baseline (uptrend confirm)."""
    obv = calculate_obv_latest(data)
    baseline = calculate_obv_ema(data, span=span)
    if math.isnan(obv) or math.isnan(baseline):
        return None
    return bool(obv > baseline)


def calculate_obv_slope(data: pd.DataFrame, window: int = 20) -> float:
    """
    Linear slope of OBV over the latest window.
    Returns NaN when close/volume are unavailable.
    """
    if data.empty or window < 2:
        return float("nan")
    obv = _obv_series(data)
    if len(obv) < 2:
        return float("nan")
    tail = obv.iloc[-min(window, len(obv)) :]
    if len(tail) < 2:
        return float("nan")
    x = np.arange(len(tail), dtype=float)
    y = tail.to_numpy(dtype=float)
    if np.all(np.isnan(y)):
        return float("nan")
    slope = np.polyfit(x, y, 1)[0]
    return float(slope)


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


def _max_oi_strike(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or "oi" not in df.columns or "strike" not in df.columns:
        return {"strike": float("nan"), "oi": float("nan")}
    work = df.copy()
    work["oi"] = pd.to_numeric(work["oi"], errors="coerce")
    work = work.dropna(subset=["oi", "strike"])
    if work.empty:
        return {"strike": float("nan"), "oi": float("nan")}
    idx = work["oi"].idxmax()
    row = work.loc[idx]
    return {"strike": float(row["strike"]), "oi": float(row["oi"])}


def find_oi_walls(option_chain_df: pd.DataFrame) -> dict[str, Any]:
    """Return strike with maximum open interest (ignores NaN OI)."""
    df = option_chain_df.copy()
    if "oi" not in df.columns or "strike" not in df.columns:
        raise ValueError("option_chain_df must contain 'strike' and 'oi' columns")
    return _max_oi_strike(df)


def find_call_put_oi_walls(
    call_chain_df: pd.DataFrame,
    put_chain_df: pd.DataFrame,
) -> dict[str, Any]:
    """Separate call-wall (max call OI strike) and put-wall (max put OI strike)."""
    call_wall = _max_oi_strike(call_chain_df.copy())
    put_wall = _max_oi_strike(put_chain_df.copy())
    combined = find_oi_walls(
        pd.concat([call_chain_df, put_chain_df], ignore_index=True)
        if not call_chain_df.empty or not put_chain_df.empty
        else pd.DataFrame(columns=["strike", "oi"])
    )
    return {
        "call_wall_strike": call_wall["strike"],
        "call_wall_oi": call_wall["oi"],
        "put_wall_strike": put_wall["strike"],
        "put_wall_oi": put_wall["oi"],
        "combined_wall_strike": combined["strike"],
        "combined_wall_oi": combined["oi"],
    }


def calculate_intent_score(pcr: float, z_score: float, absorption: float) -> float:
    """
    Map index-style technicals to 0-100: PCR + z-score + participation input.

    Intended for **NIFTY / index live** where PCR is meaningful. For single-stock
    cash equities without options context, use ``calculate_equity_technical_score``
    instead (z + volume participation only).
    """
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


def calculate_equity_technical_score(z_score: float, participation_for_scoring: float) -> float:
    """
    0-100 equity **cash-market** score: z-score + volume participation only.

    ``participation_for_scoring`` is the calibrated, score-ready volume participation
    input (same scale as passed to sector audits: typically post cap + log compress,
    comparable to the old ``absorption_for_scoring`` field).
    """
    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, x))

    def norm_z(z: float) -> float:
        if math.isnan(z):
            return 0.5
        return clamp01(0.5 + 0.5 * math.tanh(z / 3.0))

    def norm_participation(p: float) -> float:
        if math.isnan(p):
            return 0.5
        if math.isinf(p) and p > 0:
            return 1.0
        return clamp01(p / 3.0)

    w_z, w_p = 0.52, 0.48
    blended = w_z * norm_z(z_score) + w_p * norm_participation(participation_for_scoring)
    return round(100.0 * blended, 2)
