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


def test_institutional_flow_delivery_and_deals():
    from institutional_flow import score_institutional_flow

    audit = {
        "cmf_20": 0.05,
        "delivery_pct": 55.0,
        "nse_block_bulk_deals": [{"side": "BUY", "qty": 1000}, {"side": "SELL", "qty": 200}],
        "fii_holding_change_pct": 1.2,
    }
    out = score_institutional_flow(audit)
    assert out["available"] is True
    assert "delivery" in " ".join(out["reasons"]).lower()


def test_enrich_audit_institutional_data_mocked(monkeypatch):
    from institutional_flow import enrich_audit_institutional_data

    class _Cfg:
        supabase_url = "http://example"
        supabase_key = "key"

    inst = type("I", (), {"symbol": "RELIANCE", "exchange": "NSE"})()
    audit: dict = {}

    monkeypatch.setattr(
        "breakout_eod_context.load_delivery_pct_by_symbol",
        lambda symbols, **kw: {"RELIANCE": 42.5},
    )
    monkeypatch.setattr(
        "sector_priority.fetch_nse_bulk_block_deals",
        lambda symbol, **kw: {"items": [{"side": "BUY", "qty": 500}], "error": ""},
    )

    class _Res:
        data = [
            {
                "symbol": "RELIANCE",
                "promoter_holding_pct": 50.0,
                "fii_holding_change_pct": 0.8,
            }
        ]

    class _Table:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return _Res()

    class _Client:
        def table(self, _name):
            return _Table()

    monkeypatch.setattr("supabase.create_client", lambda *_a, **_k: _Client())

    flow = enrich_audit_institutional_data(audit, _Cfg(), inst)
    assert audit["delivery_pct"] == 42.5
    assert flow["available"] is True
    assert "delivery_daily" in str(flow.get("source"))


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
