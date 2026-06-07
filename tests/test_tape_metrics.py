"""Unit tests for tape_metrics helpers."""

import math

import pandas as pd

from tape_metrics import (
    benchmark_relative_returns,
    median_notional_inr_20d,
    ohlc_last_bar_as_of_date,
    pct_return_n_sessions_back,
    percentile_rank_0_100,
    sort_ohlc_by_datetime,
)


def test_percentile_rank_mid_and_edges():
    xs = [10.0, 20.0, 30.0, 40.0]
    assert percentile_rank_0_100(xs, 10.0) == 0.0
    assert percentile_rank_0_100(xs, 40.0) == 100.0
    # 25 lies strictly between 20 and 30 → empirical rank between 50 and 100 on 4 points
    assert 50.0 <= percentile_rank_0_100(xs, 25.0) <= 100.0
    assert math.isnan(percentile_rank_0_100([], 1.0))


def test_pct_return_n_sessions_back():
    s = pd.Series([100.0, 102.0, 101.0, 104.0])
    assert pct_return_n_sessions_back(s, 1) == round((104.0 / 101.0 - 1.0) * 100.0, 4)
    assert math.isnan(pct_return_n_sessions_back(s, 10))


def test_benchmark_relative_returns_stock_outperforms_flat_bench():
    n = 8
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    stock = pd.DataFrame(
        {"datetime": dates, "close": [100.0 + float(i) for i in range(n)]}
    )
    bench = pd.DataFrame({"datetime": dates, "close": [1000.0] * n})
    out = benchmark_relative_returns(stock, bench, "close", horizons=(5,))
    assert not math.isnan(out["rel_return_5d_vs_nifty_pct"])
    assert out["rel_return_5d_vs_nifty_pct"] > 0.0


def test_median_notional_inr_20d():
    df = pd.DataFrame({"close": [10.0, 12.0], "volume": [100.0, 100.0]})
    assert median_notional_inr_20d(df, "close") == 1100.0


def test_sort_ohlc_by_datetime_and_last_bar_date():
    df = pd.DataFrame(
        {
            "datetime": ["2026-06-06", "2026-06-04", "2026-06-05"],
            "close": [110.0, 100.0, 105.0],
        }
    )
    sorted_df = sort_ohlc_by_datetime(df)
    assert list(sorted_df["close"]) == [100.0, 105.0, 110.0]
    assert ohlc_last_bar_as_of_date(sorted_df).isoformat() == "2026-06-06"
