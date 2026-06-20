"""Sector-relative rank score (always-on production ranking path)."""

from __future__ import annotations


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
    )
    score_low = _score_from_features(
        bucket="small",
        ret_1w=25.0,
        ret_1m=20.0,
        absorption=1.0,
        sector_relative_rank_score=low_pct,
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


def test_without_rank_score_falls_back_to_percentile_terms():
    from sector_priority import _score_from_features

    legacy = _score_from_features(
        bucket="small",
        ret_1w=10.0,
        ret_1m=8.0,
        absorption=1.0,
        percentile_1w=80.0,
        percentile_1m=70.0,
        sector_relative_rank_score=None,
    )
    with_rank = _score_from_features(
        bucket="small",
        ret_1w=10.0,
        ret_1m=8.0,
        absorption=1.0,
        percentile_1w=80.0,
        percentile_1m=70.0,
        sector_relative_rank_score=99.0,
    )
    assert with_rank > legacy


def test_rank_score_uses_five_term_weights():
    from sector_priority import compute_sector_relative_rank_components, compute_sector_relative_rank_score

    kwargs = dict(
        sector_pctile_return_1m=80.0,
        sector_pctile_return_3m=70.0,
        sector_pctile_rel_strength=60.0,
        sector_pctile_intent=90.0,
        sector_pctile_next_week=85.0,
    )
    expected = 0.30 * 80 + 0.25 * 70 + 0.20 * 60 + 0.15 * 90 + 0.10 * 85
    assert compute_sector_relative_rank_score(**kwargs) == round(expected, 4)
    meta = compute_sector_relative_rank_components(**kwargs)
    assert meta["score"] == round(expected, 4)
    assert meta["components"]["sector_pctile_intent"]["weight"] == 0.15
    assert meta["components"]["sector_pctile_next_week"]["weighted"] == round(0.10 * 85, 4)


def test_momentum_term_always_uses_sector_relative_rank():
    from sector_priority import _score_from_features

    high_pct = _score_from_features(
        bucket="small",
        ret_1w=25.0,
        ret_1m=20.0,
        absorption=1.0,
        percentile_1w=30.0,
        percentile_1m=30.0,
        sector_pctile_return_1m=90.0,
        sector_pctile_return_3m=85.0,
        sector_pctile_rel_strength=80.0,
        sector_pctile_intent=75.0,
        sector_pctile_next_week=70.0,
    )
    low_pct = _score_from_features(
        bucket="small",
        ret_1w=5.0,
        ret_1m=4.0,
        absorption=1.0,
        percentile_1w=90.0,
        percentile_1m=85.0,
        sector_pctile_return_1m=30.0,
        sector_pctile_return_3m=25.0,
        sector_pctile_rel_strength=20.0,
        sector_pctile_intent=15.0,
        sector_pctile_next_week=10.0,
    )
    assert high_pct > low_pct


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
