"""Unit tests for institutional_flow factor."""

from __future__ import annotations

import pytest


def test_institutional_flow_accumulation():
    from institutional_flow import score_institutional_flow

    audit = {
        "cmf_20": 0.12,
        "obv_slope_20": 2.5,
        "obv_trend_confirm": True,
        "volume_participation_ratio": 1.8,
        "return_1d_pct": 1.2,
        "cmf_20_delta": {"interpretation": "strengthening"},
    }
    out = score_institutional_flow(audit)
    assert out["available"] is True
    assert out["score"] > 60.0
    assert "accumulation" in " ".join(out["reasons"]).lower() or "CMF" in out["reasons"][0]


def test_institutional_flow_missing():
    from institutional_flow import score_institutional_flow

    out = score_institutional_flow({})
    assert out["available"] is False
    assert out["score"] is None


def test_does_not_require_titan_engine():
    """Factor reads audit fields only — no CMF/OBV recompute imports."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "institutional_flow.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imports = {
        node.names[0].name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and "titan_engine" in node.module
    }
    assert not imports
