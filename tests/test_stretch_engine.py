"""Phase 5: multi-horizon stretch engine."""

from __future__ import annotations

import math

import pandas as pd


def _rising_df(n: int = 120) -> pd.DataFrame:
    closes = [100.0 + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
        }
    )


def test_composite_stretch_for_steady_leader():
    from stretch_engine import compute_stretch_composite

    comp = compute_stretch_composite(
        ema20_stretch_atr=1.5,
        ema50_stretch_atr=1.2,
        distance_from_52w_high_pct=-1.0,
    )
    assert comp < 3.0


def test_parabolic_move_detected():
    from stretch_engine import compute_stretch_composite

    comp = compute_stretch_composite(
        ema20_stretch_atr=6.0,
        ema50_stretch_atr=5.0,
        distance_from_52w_high_pct=-0.2,
    )
    assert comp > 4.0


def test_composite_preferred_over_ema200():
    from stretch_engine import effective_stretch_atr

    audit = {"ema200_stretch_atr": 8.0, "stretch_composite": 2.5}
    assert effective_stretch_atr(audit) == 2.5


def test_falls_back_to_ema200_when_composite_missing():
    from stretch_engine import effective_stretch_atr

    audit = {"ema200_stretch_atr": 4.2}
    assert effective_stretch_atr(audit) == 4.2


def test_compute_stretch_metrics_from_df():
    from stretch_engine import compute_stretch_metrics

    metrics = compute_stretch_metrics(_rising_df())
    assert not math.isnan(metrics["ema20_stretch_atr"])
    assert not math.isnan(metrics["stretch_composite"])
