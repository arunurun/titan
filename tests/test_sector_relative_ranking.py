"""Sector-relative rank score with legacy/shadow/enforce rollout."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enforce_ranking(monkeypatch):
    monkeypatch.setenv("TITAN_ENABLE_SECTOR_RELATIVE_RANKING", "1")
    monkeypatch.setenv("TITAN_SECTOR_RELATIVE_RANKING_MODE", "enforce")


def test_top_percentile_beats_large_absolute_return():
    from sector_priority import compute_sector_relative_rank_score, _score_from_features

    high_pct = compute_sector_relative_rank_score(
        sector_pctile_return_1m=90.0,
        sector_pctile_return_3m=85.0,
        sector_pctile_rel_strength=80.0,
        sector_pctile_intent=75.0,
        sector_pctile_next_week=70.0,
    )
    low_pct = compute_sector_relative_rank_score(
        sector_pctile_return_1m=30.0,
        sector_pctile_return_3m=25.0,
        sector_pctile_rel_strength=20.0,
        sector_pctile_intent=15.0,
        sector_pctile_next_week=10.0,
    )
    score_high = _score_from_features(
        bucket="small",
        ret_1w=5.0,
        ret_1m=4.0,
        absorption=1.0,
        sector_relative_rank_score=high_pct,
        ranking_mode="enforce",
    )
    score_low = _score_from_features(
        bucket="small",
        ret_1w=25.0,
        ret_1m=20.0,
        absorption=1.0,
        sector_relative_rank_score=low_pct,
        ranking_mode="enforce",
    )
    assert score_high > score_low


def test_score_bounded_0_100():
    from sector_priority import compute_sector_relative_rank_score

    assert compute_sector_relative_rank_score(
        sector_pctile_return_1m=100.0,
        sector_pctile_return_3m=100.0,
        sector_pctile_rel_strength=100.0,
        sector_pctile_intent=100.0,
        sector_pctile_next_week=100.0,
    ) == 100.0
    assert compute_sector_relative_rank_score(
        sector_pctile_return_1m=0.0,
        sector_pctile_return_3m=0.0,
        sector_pctile_rel_strength=0.0,
        sector_pctile_intent=0.0,
        sector_pctile_next_week=0.0,
    ) == 0.0


def test_sector_rally_preserves_relative_order():
    from sector_priority import compute_sector_relative_rank_score

    a = compute_sector_relative_rank_score(
        sector_pctile_return_1m=80.0,
        sector_pctile_return_3m=70.0,
        sector_pctile_rel_strength=60.0,
        sector_pctile_intent=55.0,
        sector_pctile_next_week=50.0,
    )
    b = compute_sector_relative_rank_score(
        sector_pctile_return_1m=40.0,
        sector_pctile_return_3m=35.0,
        sector_pctile_rel_strength=30.0,
        sector_pctile_intent=25.0,
        sector_pctile_next_week=20.0,
    )
    assert a > b
    assert a - b > 20.0


def test_intent_and_next_week_weighted_in_rank_score():
    from sector_priority import compute_sector_relative_rank_score

    intent_led = compute_sector_relative_rank_score(
        sector_pctile_return_1m=50.0,
        sector_pctile_return_3m=50.0,
        sector_pctile_rel_strength=50.0,
        sector_pctile_intent=90.0,
        sector_pctile_next_week=85.0,
    )
    flat = compute_sector_relative_rank_score(
        sector_pctile_return_1m=50.0,
        sector_pctile_return_3m=50.0,
        sector_pctile_rel_strength=50.0,
        sector_pctile_intent=50.0,
        sector_pctile_next_week=50.0,
    )
    assert intent_led > flat


def test_legacy_mode_uses_percentile_1w_1m_terms(monkeypatch):
    from sector_priority import _score_from_features

    monkeypatch.delenv("TITAN_ENABLE_SECTOR_RELATIVE_RANKING", raising=False)
    legacy = _score_from_features(
        bucket="small",
        ret_1w=10.0,
        ret_1m=8.0,
        absorption=1.0,
        percentile_1w=80.0,
        percentile_1m=70.0,
        sector_relative_rank_score=99.0,
        ranking_mode="off",
    )
    with_rank = _score_from_features(
        bucket="small",
        ret_1w=10.0,
        ret_1m=8.0,
        absorption=1.0,
        percentile_1w=80.0,
        percentile_1m=70.0,
        sector_relative_rank_score=99.0,
        ranking_mode="enforce",
    )
    assert with_rank > legacy


def test_momentum_score_differs_from_rank_score():
    from sector_priority import (
        compute_sector_relative_momentum_score,
        compute_sector_relative_rank_score,
    )

    kwargs = dict(
        sector_pctile_return_1m=62.0,
        sector_pctile_return_3m=58.0,
        sector_pctile_rel_strength=71.0,
        sector_pctile_intent=66.0,
        sector_pctile_next_week=64.0,
    )
    assert compute_sector_relative_momentum_score(**kwargs) != compute_sector_relative_rank_score(**kwargs)
