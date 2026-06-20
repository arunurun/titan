"""Phase 7: backtest performance metrics."""

from __future__ import annotations

import math

import pytest

from analytics.performance_metrics import (
    average_loser,
    average_winner,
    expectancy,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    summarize_returns,
)


def test_profit_factor_hand_calc():
    returns = [10.0, -5.0, 8.0, -4.0]
    # wins=18, losses=9 -> PF=2
    assert profit_factor(returns) == pytest.approx(2.0)


def test_expectancy_hand_calc():
    returns = [10.0, -5.0, 8.0, -4.0]
    assert expectancy(returns) == pytest.approx(2.25)


def test_average_winner_loser():
    returns = [10.0, -5.0, 8.0, -4.0]
    assert average_winner(returns) == pytest.approx(9.0)
    assert average_loser(returns) == pytest.approx(-4.5)


def test_max_drawdown_hand_calc():
    equity = [100.0, 110.0, 99.0, 105.0]
    # peak 110 -> trough 99 => dd = 11/110
    assert max_drawdown(equity) == pytest.approx(11.0 / 110.0)


def test_sharpe_positive_on_consistent_wins():
    returns = [2.0, 2.5, 1.8, 2.2, 2.1]
    assert sharpe_ratio(returns) > 0.0


def test_summarize_returns_bundle():
    returns = [5.0, -3.0, 4.0, -2.0, 6.0]
    summary = summarize_returns(returns)
    assert summary["n_trades"] == 5.0
    assert not math.isnan(summary["profit_factor"])
