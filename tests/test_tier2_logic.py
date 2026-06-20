"""Phase 3: sector-aware Tier-2 corroboration."""

from __future__ import annotations


def _base_audit(**overrides):
    audit = {
        "return_1d_pct": 0.5,
        "median_notional_inr_20d": 5_000_000.0,
        "cmf_20": 0.1,
        "adx_14": 30.0,
        "adx_plus_di_14": 25.0,
        "adx_minus_di_14": 15.0,
        "rel_return_5d_vs_nifty_pct": 2.0,
        "sector_key": "pharma_healthcare",
    }
    audit.update(overrides)
    return audit


def _hot_c(overext: bool = True) -> dict:
    return {"over_extension_hot": overext, "families": {}, "trace": []}


def _empty_d() -> dict:
    return {"staleflow_downgrade": False}


def test_overextension_only_no_trim():
    from signal_v2 import layer_b

    b = layer_b(_base_audit(), _hot_c(True), _empty_d())
    assert b["forced_label"] is None


def test_overextension_plus_distribution_trims():
    from signal_v2 import layer_b

    b = layer_b(_base_audit(cmf_20=-0.1), _hot_c(True), _empty_d())
    assert b["forced_label"] == "trim"


def test_momentum_sector_requires_higher_corroboration():
    from signal_v2 import _tier2_thresholds

    trim, exit_c = _tier2_thresholds({"sector_key": "defence"})
    assert trim == 3
    assert exit_c == 4
    trim_sub, exit_sub = _tier2_thresholds({"sector_key": "india_defence_stocks"})
    assert trim_sub == 3
    assert exit_sub == 4
    trim2, exit2 = _tier2_thresholds({"sector_key": "pharma_healthcare"})
    assert trim2 == 2
    assert exit2 == 3
