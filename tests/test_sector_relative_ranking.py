"""Phase 1: sector-relative momentum ranking."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("TITAN_USE_SECTOR_RELATIVE_RANKING", raising=False)


def test_top_percentile_beats_large_absolute_return(monkeypatch):
    from sector_priority import compute_sector_relative_momentum_score, _score_from_features, use_sector_relative_ranking

    high_pct = compute_sector_relative_momentum_score(
        sector_pctile_return_1m=90.0,
        sector_pctile_return_3m=85.0,
        sector_pctile_rel_strength=80.0,
        sector_pctile_intent=75.0,
        sector_pctile_next_week=70.0,
    )
    low_pct = compute_sector_relative_momentum_score(
        sector_pctile_return_1m=30.0,
        sector_pctile_return_3m=25.0,
        sector_pctile_rel_strength=20.0,
        sector_pctile_intent=15.0,
        sector_pctile_next_week=10.0,
    )
    monkeypatch.setenv("TITAN_USE_SECTOR_RELATIVE_RANKING", "true")
    assert use_sector_relative_ranking()
    score_high = _score_from_features(
        bucket="small",
        ret_1w=5.0,
        ret_1m=4.0,
        absorption=1.0,
        sector_relative_score=high_pct,
    )
    score_low = _score_from_features(
        bucket="small",
        ret_1w=25.0,
        ret_1m=20.0,
        absorption=1.0,
        sector_relative_score=low_pct,
    )
    assert score_high > score_low


def test_score_bounded_0_100():
    from sector_priority import compute_sector_relative_momentum_score

    assert compute_sector_relative_momentum_score(
        sector_pctile_return_1m=100.0,
        sector_pctile_return_3m=100.0,
        sector_pctile_rel_strength=100.0,
        sector_pctile_intent=100.0,
        sector_pctile_next_week=100.0,
    ) == 100.0
    assert compute_sector_relative_momentum_score(
        sector_pctile_return_1m=0.0,
        sector_pctile_return_3m=0.0,
        sector_pctile_rel_strength=0.0,
        sector_pctile_intent=0.0,
        sector_pctile_next_week=0.0,
    ) == 0.0


def test_sector_rally_preserves_relative_order():
    from sector_priority import compute_sector_relative_momentum_score

    a = compute_sector_relative_momentum_score(
        sector_pctile_return_1m=80.0,
        sector_pctile_return_3m=70.0,
        sector_pctile_rel_strength=60.0,
        sector_pctile_intent=55.0,
        sector_pctile_next_week=50.0,
    )
    b = compute_sector_relative_momentum_score(
        sector_pctile_return_1m=40.0,
        sector_pctile_return_3m=35.0,
        sector_pctile_rel_strength=30.0,
        sector_pctile_intent=25.0,
        sector_pctile_next_week=20.0,
    )
    assert a > b
    assert a - b > 20.0


def test_flag_off_uses_legacy_percentile_terms(monkeypatch):
    from sector_priority import _score_from_features, use_sector_relative_ranking

    assert not use_sector_relative_ranking()
    top = _score_from_features(
        bucket="small",
        ret_1w=20.0,
        ret_1m=15.0,
        absorption=1.0,
        percentile_1w=100.0,
        percentile_1m=100.0,
        sector_relative_score=10.0,
    )
    mid = _score_from_features(
        bucket="small",
        ret_1w=20.0,
        ret_1m=15.0,
        absorption=1.0,
        percentile_1w=50.0,
        percentile_1m=50.0,
        sector_relative_score=90.0,
    )
    assert top > mid
