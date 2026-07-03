"""Fusion pipeline ordering: dampener, sector rollups, fundamentals graceful skip."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest

from config_loader import TitanConfig
from sector_registry import SectorInstrument


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def _base_scores() -> dict:
    return {
        "technical": {"score": 60.0, "confidence": 0.9, "available": True, "reasons": [], "metadata": {}},
        "relative_strength": {"score": 55.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
        "institutional_flow": {"score": 50.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "fundamentals": {"score": 50.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "market_regime": {"score": 55.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
        "sector_strength": {"score": 52.0, "confidence": 0.7, "available": True, "reasons": [], "metadata": {}},
        "risk": {"score": 60.0, "confidence": 0.8, "available": True, "reasons": [], "metadata": {}},
    }


def test_refresh_runs_dampener_before_fusion(monkeypatch):
    from sector_audit import _refresh_symbol_scoring_outputs

    order: list[str] = []

    def _dampener(audit):
        order.append("dampener")
        audit["effective_intent_score"] = 55.0

    def _fusion(audit):
        order.append("fusion")
        tech = audit.get("factor_scores", {}).get("technical", {})
        order.append(f"tech_at_fusion={tech.get('score')}")
        audit["titan_score"] = 60.0
        return {"titan_score": 60.0}

    populate_calls: list[float | None] = []

    def _populate(audit, **kwargs):
        populate_calls.append(audit.get("effective_intent_score"))
        eff = audit.get("effective_intent_score")
        if isinstance(audit.get("factor_scores"), dict) and eff is not None:
            audit["factor_scores"]["technical"]["score"] = float(eff)

    monkeypatch.setattr("sector_audit._apply_contemporaneous_dampener", _dampener)
    monkeypatch.setattr("titan_fusion.apply_fusion_to_audit", _fusion)
    monkeypatch.setattr("sector_audit._populate_factor_scores", _populate)

    audit = {
        "factor_scores": copy.deepcopy(_base_scores()),
        "effective_intent_score": 70.0,
        "return_1d_pct": 5.0,
        "z_score": 2.0,
        "fundamental_status": "neutral",
    }
    _refresh_symbol_scoring_outputs(audit)

    assert order[0] == "dampener"
    assert order[1] == "fusion"
    assert populate_calls == [70.0, 55.0]
    assert order[2] == "tech_at_fusion=55.0"


def test_refresh_uses_sector_rollups_before_fusion(monkeypatch):
    from sector_audit import _refresh_symbol_scoring_outputs

    rollups = [
        {"sector": "defence", "avg_effective_intent_score": 80.0},
        {"sector": "ai", "avg_effective_intent_score": 40.0},
    ]
    captured: dict = {}

    def _fusion(audit):
        fs = audit.get("factor_scores") or {}
        captured["sector_strength"] = fs.get("sector_strength")
        captured["rs"] = fs.get("relative_strength")
        audit["titan_score"] = 65.0
        return {"titan_score": 65.0}

    monkeypatch.setattr("titan_fusion.apply_fusion_to_audit", _fusion)

    audit = {
        "factor_scores": copy.deepcopy(_base_scores()),
        "sector": "defence",
        "effective_intent_score": 62.0,
        "rel_return_20d_vs_nifty_pct": 3.0,
        "sector_relative_strength_pctile": 85.0,
        "_sector_rotation_rollups": rollups,
        "return_1d_pct": 0.5,
        "z_score": 0.5,
        "fundamental_status": "neutral",
    }
    _refresh_symbol_scoring_outputs(audit)

    assert captured["sector_strength"]["available"] is True
    assert captured["rs"]["available"] is True
    assert audit.get("sector_rotation", {}).get("available") is True


def test_populate_factor_scores_lazy_loads_fundamentals(monkeypatch):
    from sector_audit import _populate_factor_scores

    monkeypatch.setattr(
        "sector_audit.load_config",
        lambda **kwargs: make_cfg(),
    )
    monkeypatch.setattr(
        "fundamental_engine.assess_fundamental_strength",
        lambda _cfg, _inst: {
            "status": "strong",
            "score": 72.0,
            "reasons": ["ROE strong"],
            "factor": {
                "score": 72.0,
                "confidence": 0.8,
                "reasons": ["ROE strong"],
                "metadata": {"status": "strong"},
                "available": True,
            },
        },
    )

    audit = {
        "symbol": "HAL",
        "exchange": "NSE",
        "effective_intent_score": 60.0,
    }
    _populate_factor_scores(audit)

    assert audit["fundamental_score"] == 72.0
    assert audit["factor_scores"]["fundamentals"]["available"] is True
    assert audit["factor_scores"]["fundamentals"]["score"] == 72.0


def test_populate_factor_scores_graceful_skip_when_fundamentals_missing(monkeypatch):
    from sector_audit import _populate_factor_scores

    monkeypatch.setattr("sector_audit.load_config", lambda **kwargs: make_cfg())
    monkeypatch.setattr(
        "fundamental_engine.assess_fundamental_strength",
        lambda _cfg, _inst: {"status": "unavailable", "score": None, "reasons": ["fundamental row missing"]},
    )

    audit = {"symbol": "MISSING", "exchange": "NSE", "effective_intent_score": 50.0}
    _populate_factor_scores(audit)

    assert audit["factor_scores"]["fundamentals"]["available"] is False
    assert audit["factor_scores"]["fundamentals"]["score"] is None


def test_build_equity_live_audit_defer_scoring_outputs(monkeypatch):
    import pandas as pd

    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "high": closes, "low": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "sector_audit._assess_fundamental_strength",
        lambda *_a, **_k: {"status": "unavailable", "score": None, "reasons": []},
    )
    refresh_called: list[bool] = []

    def _track_refresh(audit):
        refresh_called.append(True)
        audit["next_week_score"] = 55.0
        audit["titan_score"] = 50.0

    monkeypatch.setattr("sector_audit._refresh_symbol_scoring_outputs", _track_refresh)

    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(
        make_cfg(),
        MagicMock(),
        inst,
        sector_id="defence",
        with_narrative=False,
        defer_scoring_outputs=True,
    )
    assert refresh_called == []
    assert "next_week_score" not in audit
    assert "titan_score" not in audit
    assert isinstance(audit.get("factor_scores"), dict)


def test_digest_batch_applies_sector_inputs_before_authoritative_refresh(monkeypatch):
    from sector_audit import _apply_sector_cross_section, _apply_sector_rotation_scores, _refresh_symbol_scoring_outputs

    fusion_sector_scores: list[bool] = []

    def _fusion(audit):
        fs = audit.get("factor_scores") or {}
        ss = fs.get("sector_strength") or {}
        rs = fs.get("relative_strength") or {}
        fusion_sector_scores.append(bool(ss.get("available") and rs.get("available")))
        audit["titan_score"] = 60.0
        return {"titan_score": 60.0}

    monkeypatch.setattr("titan_fusion.apply_fusion_to_audit", _fusion)

    ok_results = [
        {
            "audit": {
                "factor_scores": copy.deepcopy(_base_scores()),
                "sector": "defence",
                "effective_intent_score": 65.0,
                "rel_return_20d_vs_nifty_pct": 4.0,
                "rel_return_5d_vs_nifty_pct": 2.0,
                "return_1d_pct": 0.5,
                "z_score": 1.0,
                "atr_14_pct": 2.0,
                "median_notional_inr_20d": 5e6,
                "fundamental_status": "neutral",
            }
        },
        {
            "audit": {
                "factor_scores": copy.deepcopy(_base_scores()),
                "sector": "defence",
                "effective_intent_score": 55.0,
                "rel_return_20d_vs_nifty_pct": -1.0,
                "rel_return_5d_vs_nifty_pct": -0.5,
                "return_1d_pct": -0.2,
                "z_score": -0.3,
                "atr_14_pct": 2.5,
                "median_notional_inr_20d": 4e6,
                "fundamental_status": "neutral",
            }
        },
    ]
    rollups = [
        {"sector": "defence", "avg_effective_intent_score": 70.0},
        {"sector": "ai", "avg_effective_intent_score": 45.0},
    ]
    monkeypatch.setattr(
        "analysis_store.build_rotation_sector_rollups",
        lambda *_a, **_k: rollups,
    )

    _apply_sector_cross_section(ok_results, score_percentiles=False)
    _apply_sector_rotation_scores(make_cfg(), "defence", ok_results)
    for row in ok_results:
        _refresh_symbol_scoring_outputs(row["audit"])

    assert fusion_sector_scores == [True, True]
    for row in ok_results:
        assert row["audit"].get("sector_relative_strength_pctile") is not None
        assert row["audit"].get("_sector_rotation_rollups") == rollups
