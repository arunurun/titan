"""Phase 0: titan_fusion engine tests."""

from __future__ import annotations

import json

import pytest


def _comp(
    key: str,
    score: float | None,
    *,
    available: bool | None = None,
    confidence: float = 0.9,
) -> dict:
    from titan_fusion import FusionComponent

    is_avail = available if available is not None else score is not None
    return FusionComponent(
        key=key,
        score=score,
        confidence=confidence,
        reason="test",
        available=bool(is_avail and score is not None),
        metadata={},
    )


def _user_example_components() -> dict:
    return {
        "technical": _comp("technical", 84.0),
        "relative_strength": _comp("relative_strength", 95.0),
        "institutional_flow": _comp("institutional_flow", 92.0),
        "fundamentals": _comp("fundamentals", 70.0),
        "market_regime": _comp("market_regime", 78.0),
        "sector_strength": _comp("sector_strength", 81.0),
        "risk": _comp("risk", 80.0),
    }


def test_user_example_exact():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    result = fuse_titan_score(_user_example_components(), weights=DEFAULT_FUSION_WEIGHTS)
    assert result["titan_score"] == pytest.approx(84.3, abs=0.05)
    assert result["technical_score"] == 84.0
    assert result["relative_strength_score"] == 95.0
    assert result["flow_score"] == 92.0
    assert result["fundamental_score"] == 70.0
    assert result["regime_score"] == 78.0
    assert result["sector_score"] == 81.0
    assert result["risk_score"] == 80.0


def test_contributions_math():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    result = fuse_titan_score(_user_example_components(), weights=DEFAULT_FUSION_WEIGHTS)
    assert result["contributions"]["technical"]["weighted"] == pytest.approx(25.2, abs=0.01)


def test_explanation_format():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    result = fuse_titan_score(_user_example_components(), weights=DEFAULT_FUSION_WEIGHTS)
    text = result["overall_explanation"]
    assert "84" in text
    assert "30%" in text
    assert "25.2" in text
    assert "Total" in text
    assert "84.3" in text


def test_missing_institutional_flow_redistribution():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    components = _user_example_components()
    components["institutional_flow"] = _comp("institutional_flow", None, available=False, confidence=0.0)

    result = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    assert result["weights"]["missing_keys"] == ["institutional_flow"]
    eff = result["weights"]["effective"]
    assert pytest.approx(sum(eff.values()), abs=1e-6) == 1.0
    assert eff["technical"] > DEFAULT_FUSION_WEIGHTS["technical"]
    assert result["titan_score"] is not None
    assert result["coverage"] == pytest.approx(0.85, abs=0.001)


def test_missing_three_pillars():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    components = _user_example_components()
    for key in ("institutional_flow", "fundamentals", "sector_strength"):
        components[key] = _comp(key, None, available=False)

    result = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    # flow 15% + fundamentals 15% + sector 5% = 35% missing → coverage 65%
    assert result["coverage"] == pytest.approx(0.65, abs=0.001)
    assert pytest.approx(sum(result["weights"]["effective"].values()), abs=1e-6) == 1.0


def test_all_missing():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    components = {
        pillar: _comp(pillar, None, available=False, confidence=0.0)
        for pillar in DEFAULT_FUSION_WEIGHTS
    }
    result = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    assert result["titan_score"] is None
    assert result["overall_confidence"] == 0.0
    assert "no fusion components available" in result["overall_explanation"]


def test_partial_confidence():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    components = _user_example_components()
    components["technical"]["confidence"] = 0.4
    components["risk"]["confidence"] = 0.95
    result = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    assert 0.0 < result["overall_confidence"] < 1.0


def test_zero_confidence_one_pillar():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    full = fuse_titan_score(_user_example_components(), weights=DEFAULT_FUSION_WEIGHTS)
    components = _user_example_components()
    components["technical"]["confidence"] = 0.0
    reduced = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    assert reduced["overall_confidence"] < full["overall_confidence"]


def test_weight_auto_normalize(monkeypatch):
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, FUSION_PILLARS, fuse_titan_score, load_fusion_weights

    monkeypatch.setenv(
        "TITAN_FUSION_WEIGHTS_JSON",
        json.dumps({p: DEFAULT_FUSION_WEIGHTS[p] * 0.95 for p in FUSION_PILLARS}),
    )
    weights = load_fusion_weights()
    pillar_weights = {p: weights[p] for p in FUSION_PILLARS}
    assert pytest.approx(sum(pillar_weights.values()), abs=1e-6) == 1.0
    assert weights.get("_fusion_weight_meta", {}).get("normalized") is True
    result = fuse_titan_score(_user_example_components(), weights=pillar_weights)
    assert result["titan_score"] == pytest.approx(84.3, abs=0.1)


def test_breadth_not_weighted():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    components = _user_example_components()
    without = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS)
    breadth = _comp("breadth", 99.0)
    with_breadth = fuse_titan_score(components, weights=DEFAULT_FUSION_WEIGHTS, breadth=breadth)
    assert without["titan_score"] == with_breadth["titan_score"]
    assert with_breadth["breadth_score"] == 99.0


def test_breadth_in_output():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_titan_score

    breadth = _comp("breadth", 72.0)
    result = fuse_titan_score(_user_example_components(), weights=DEFAULT_FUSION_WEIGHTS, breadth=breadth)
    assert result["breadth_score"] == 72.0
    assert result["breadth"] is not None


def test_batch_parity():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, fuse_batch, fuse_titan_score

    rows = [_user_example_components(), _user_example_components()]
    batch = fuse_batch(rows, weights=DEFAULT_FUSION_WEIGHTS)
    for i, row in enumerate(rows):
        single = fuse_titan_score(row, weights=DEFAULT_FUSION_WEIGHTS)
        assert batch[i]["titan_score"] == single["titan_score"]
        assert batch[i]["overall_confidence"] == single["overall_confidence"]


def test_audit_adapter_technical_only():
    from titan_fusion import fuse_from_audit

    audit = {"effective_intent_score": 84.0}
    result = fuse_from_audit(audit)
    assert result["technical_score"] == 84.0
    assert result["titan_score"] is not None
    assert result["weights"]["missing_keys"]


def test_apply_fusion_always_on():
    from titan_fusion import apply_fusion_to_audit

    audit = {
        "effective_intent_score": 84.0,
        "sector_relative_strength_pctile": 81.0,
        "fundamental_score": 70.0,
        "market_regime": {"regime": "BULL"},
        "next_week_score": 70.0,
    }
    out = apply_fusion_to_audit(audit)
    assert out is not None
    assert "titan_fusion" in audit
    assert "titan_score" in audit
    assert "fusion_confidence" in audit
    assert audit["next_week_score"] == 70.0
    assert audit["titan_score"] == out["titan_score"]


def test_load_recommended_weights_from_default_paths(monkeypatch, tmp_path):
    from titan_fusion import FUSION_PILLARS, load_fusion_weights

    weights_path = tmp_path / "data" / "recommended_weights.json"
    weights_path.parent.mkdir(parents=True)
    weights_path.write_text(
        json.dumps(
            {
                "weights": {p: (0.2 if p == "technical" else 0.8 / (len(FUSION_PILLARS) - 1)) for p in FUSION_PILLARS}
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "titan_fusion._RECOMMENDED_WEIGHTS_SEARCH_PATHS",
        (str(weights_path),),
    )
    loaded = load_fusion_weights()
    assert loaded["technical"] == pytest.approx(0.2, abs=0.01)


def test_risk_no_signal_v2_import():
    import ast
    import titan_fusion

    module_path = titan_fusion.__file__
    with open(module_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    import_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    import_from_names = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {"signal_v2", "action_signals", "sector_audit", "probability_calibration"}
    assert forbidden.isdisjoint(import_names)
    assert forbidden.isdisjoint(import_from_names)

    audit = {"trap_exit_proxy": True}
    result = titan_fusion.fuse_from_audit(audit)
    assert result["risk_score"] == 75.0


def test_no_buy_sell_in_output():
    from titan_fusion import fuse_from_audit

    audit = {"effective_intent_score": 84.0, "action_signal": "buy"}
    result = fuse_from_audit(audit)
    for forbidden in ("action_signal", "sell_signal", "forced_label"):
        assert forbidden not in result


def test_regime_label_mapping():
    from titan_fusion import REGIME_SCORE_MAP, fuse_from_audit

    assert REGIME_SCORE_MAP["STRONG_BULL"] == 90.0
    assert REGIME_SCORE_MAP["DEFENSIVE"] == 40.0
    assert REGIME_SCORE_MAP["CORRECTION"] == 40.0

    audit = {"market_regime": {"regime": "STRONG_BULL"}}
    result = fuse_from_audit(audit)
    assert result["regime_score"] == 90.0
