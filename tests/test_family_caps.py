"""Family risk caps in signal_v2 aggregation."""

from __future__ import annotations

import signal_v2 as v2


def test_correlated_price_terms_capped():
    audit: dict = {}
    fam = {"horizon": 1.0, "intent": 1.0, "z": 2.0, "trend": 2.0, "momentum": 3.0, "volatility": 0.0}
    c = {
        "families": fam,
        "money_flow_bear": 0.0,
        "over_extension": 0.0,
        "upside_z": 0.0,
        "fundamental": 0.0,
        "money_flow_bull": 0.0,
    }
    d = {"mult_momentum": 1.0, "mult_money_flow": 1.0, "mult_over_extension": 1.0, "mult_risk": 1.0}
    agg = v2._aggregate(c, d, audit)
    assert agg["risk_c"] <= 4.0 + 2.0  # price cap + horizon + intent
    assert audit["family_caps"]["groups"]["price"] <= 4.0 + 1e-9
    assert audit["family_caps"]["limits"]["price"] == 4.0


def test_flow_and_extension_caps():
    _, groups = v2._apply_family_caps_to_risk(
        fam={"horizon": 0.0, "intent": 0.0, "z": 0.0, "trend": 0.0},
        momentum_scaled=0.0,
        money_flow_bear=4.0,
        over_extension=2.5,
        upside_z=1.0,
        volatility=3.5,
        fundamental=0.0,
    )
    assert groups["flow"] <= 2.0 + 1e-9
    assert groups["extension"] <= 2.0 + 1e-9
    assert groups["volatility"] <= 2.0 + 1e-9


def test_correlated_signals_always_capped_before_sum():
    audit: dict = {}
    c = {
        "families": {
            "horizon": 0.0,
            "intent": 0.0,
            "z": 1.5,
            "trend": 2.0,
            "momentum": 3.0,
            "volatility": 2.5,
        },
        "money_flow_bear": 3.0,
        "over_extension": 2.0,
        "upside_z": 1.5,
        "fundamental": 0.0,
        "money_flow_bull": 0.0,
    }
    d = {"mult_momentum": 1.0, "mult_money_flow": 1.0, "mult_over_extension": 1.0, "mult_risk": 1.0}
    agg = v2._aggregate(c, d, audit)
    groups = audit["family_caps"]["groups"]
    uncapped = audit["family_caps"]["uncapped_groups"]
    uncapped_sum = sum(uncapped.values())
    assert sum(groups.values()) < uncapped_sum
    assert groups["price"] <= 4.0 + 1e-9
    assert groups["flow"] <= 2.0 + 1e-9
    assert groups["extension"] <= 2.0 + 1e-9
    assert groups["volatility"] <= 2.0 + 1e-9
    assert agg["risk_c"] == min(10.0, sum(groups.values()))
