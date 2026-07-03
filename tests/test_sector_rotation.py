"""Unit tests for sector_rotation factor."""

from __future__ import annotations

import pytest


def test_rank_sectors():
    from sector_rotation import rank_sectors

    rollups = [
        {"sector_key": "ai", "sector_relative_rank_score": 85.0},
        {"sector_key": "pharma", "sector_relative_rank_score": 55.0},
        {"sector_key": "auto", "sector_relative_rank_score": 70.0},
    ]
    ranking = rank_sectors(rollups)
    assert ranking["top_sector"] == "ai"
    assert ranking["bottom_sector"] == "pharma"
    assert "ai" in ranking["leading_sectors"]


def test_score_sector_rotation_leader():
    from sector_rotation import score_sector_rotation

    rollups = [
        {"sector_key": "ai", "avg_effective_intent_score": 80.0},
        {"sector_key": "pharma", "avg_effective_intent_score": 50.0},
    ]
    out = score_sector_rotation(rollups, "ai")
    assert out["available"] is True
    assert out["score"] == 100.0
    assert "leading" in " ".join(out["reasons"]).lower()


def test_score_sector_rotation_missing():
    from sector_rotation import score_sector_rotation

    out = score_sector_rotation([], "ai")
    assert out["available"] is False
