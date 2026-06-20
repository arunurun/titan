"""Layer D audit message formatting."""

from __future__ import annotations

import signal_v2 as v2


def test_strong_adx_reason_includes_both_di_values():
    audit = {
        "adx_14": 32.1,
        "adx_plus_di_14": 28.4,
        "adx_minus_di_14": 14.2,
        "cmf_20": 0.0,
        "return_1d_pct": 0.5,
        "return_5d_pct": 1.0,
    }
    c = v2.layer_c(audit)
    d = v2.layer_d(audit, c)
    joined = " | ".join(d["reasons"])
    assert "strong ADX 32.1 (+DI=28.4, -DI=14.2)" in joined


def test_bearish_adx_reason_includes_both_di_values():
    audit = {
        "adx_14": 30.0,
        "adx_plus_di_14": 10.0,
        "adx_minus_di_14": 22.0,
        "cmf_20": 0.0,
        "return_1d_pct": -0.5,
        "return_5d_pct": -1.0,
    }
    c = v2.layer_c(audit)
    d = v2.layer_d(audit, c)
    assert any("+DI=10.0, -DI=22.0" in r for r in d["reasons"])
