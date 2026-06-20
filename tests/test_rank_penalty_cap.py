"""Phase 4: v2 rank penalty cap and default max."""

from __future__ import annotations

import pytest

from sector_priority import _V2_RANK_PENALTY_MAX, _v2_rank_adjustment, cap_rank_penalty_families


def test_default_penalty_max_is_three():
    assert _V2_RANK_PENALTY_MAX == 3.0


def test_penalty_capped_at_max():
    out = _v2_rank_adjustment({"label": "exit-risk", "risk_net": 10.0})
    assert out["adjustment"] == pytest.approx(-3.0, abs=0.01)


def test_family_cap_limits_dominant_source():
    families = {"momentum": 4.0, "volatility": 2.0, "intent": 1.0}
    capped = cap_rank_penalty_families(families)
    total = sum(families.values())
    max_each = total * 0.30
    assert capped["momentum"] <= max_each + 1e-9
    assert sum(capped.values()) < total


def test_rank_penalty_does_not_collapse_from_one_family():
    plain = _v2_rank_adjustment({"label": "trim", "risk_net": 7.0}, overextension_penalty=0.0)
    capped = _v2_rank_adjustment(
        {"label": "trim", "risk_net": 7.0},
        overextension_penalty=0.0,
        penalty_families={"momentum": 6.0, "volatility": 0.5},
    )
    assert capped["adjustment"] > plain["adjustment"]
