"""Backtest performance metrics: profit factor, expectancy, Sharpe, drawdown."""

from __future__ import annotations

import math
from typing import Sequence


def _valid_returns(returns: Sequence[float]) -> list[float]:
    out: list[float] = []
    for r in returns:
        try:
            x = float(r)
        except (TypeError, ValueError):
            continue
        if math.isnan(x):
            continue
        out.append(x)
    return out


def profit_factor(returns: Sequence[float]) -> float:
    """Gross wins / gross losses (absolute). Returns inf when no losses."""
    rs = _valid_returns(returns)
    wins = sum(r for r in rs if r > 0)
    losses = abs(sum(r for r in rs if r < 0))
    if losses == 0:
        return float("inf") if wins > 0 else float("nan")
    return wins / losses


def expectancy(returns: Sequence[float]) -> float:
    """Average return per trade."""
    rs = _valid_returns(returns)
    return sum(rs) / len(rs) if rs else float("nan")


def average_winner(returns: Sequence[float]) -> float:
    wins = [r for r in _valid_returns(returns) if r > 0]
    return sum(wins) / len(wins) if wins else float("nan")


def average_loser(returns: Sequence[float]) -> float:
    losses = [r for r in _valid_returns(returns) if r < 0]
    return sum(losses) / len(losses) if losses else float("nan")


def sharpe_ratio(returns: Sequence[float], *, risk_free: float = 0.0) -> float:
    """Simple Sharpe: mean(excess) / std(excess). NaN when std=0."""
    rs = _valid_returns(returns)
    if len(rs) < 2:
        return float("nan")
    excess = [r - risk_free for r in rs]
    mu = sum(excess) / len(excess)
    var = sum((x - mu) ** 2 for x in excess) / (len(excess) - 1)
    sigma = math.sqrt(var) if var > 0 else 0.0
    if sigma == 0.0:
        return float("nan")
    return mu / sigma


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Peak-to-trough drawdown as a positive fraction (0.15 = 15% drawdown)."""
    if not equity_curve:
        return float("nan")
    peak = float("-inf")
    max_dd = 0.0
    for v in equity_curve:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(x):
            continue
        peak = max(peak, x)
        if peak > 0:
            dd = (peak - x) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def summarize_returns(returns: Sequence[float], *, risk_free: float = 0.0) -> dict[str, float]:
    """Compute all headline metrics for a return series."""
    rs = _valid_returns(returns)
    eq = []
    cum = 1.0
    for r in rs:
        cum *= 1.0 + r / 100.0
        eq.append(cum)
    return {
        "profit_factor": profit_factor(rs),
        "expectancy": expectancy(rs),
        "sharpe": sharpe_ratio(rs, risk_free=risk_free),
        "avg_winner": average_winner(rs),
        "avg_loser": average_loser(rs),
        "max_drawdown": max_drawdown(eq) if eq else float("nan"),
        "n_trades": float(len(rs)),
    }
