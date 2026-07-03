"""CI guardrails — fusion path must not duplicate factor work or import orchestrators."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

_FACTOR_MODULES = (
    "institutional_flow",
    "relative_strength",
    "fundamental_engine",
    "breadth_engine",
    "market_regime",
    "sector_rotation",
    "prediction_engine",
)

_FORBIDDEN_ORCHESTRATOR_IMPORTS = frozenset(
    {"signal_v2", "sector_audit", "action_signals", "probability_calibration"}
)


def _module_imports(module_name: str) -> tuple[set[str], set[str]]:
    path = SRC / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plain = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    from_mods = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    return plain, from_mods


@pytest.mark.parametrize("module_name", _FACTOR_MODULES)
def test_factor_modules_avoid_signal_orchestrator_imports(module_name: str):
    plain, from_mods = _module_imports(module_name)
    assert _FORBIDDEN_ORCHESTRATOR_IMPORTS.isdisjoint(plain)
    assert _FORBIDDEN_ORCHESTRATOR_IMPORTS.isdisjoint(from_mods)


def test_institutional_flow_does_not_import_titan_engine():
    _, from_mods = _module_imports("institutional_flow")
    assert not any("titan_engine" in m for m in from_mods)


def test_breadth_not_weighted_in_fusion_score():
    from titan_fusion import DEFAULT_FUSION_WEIGHTS, FUSION_PILLARS, fuse_titan_score

    assert "breadth" not in FUSION_PILLARS
    assert "breadth" not in DEFAULT_FUSION_WEIGHTS

    components = {
        pillar: {
            "key": pillar,
            "score": 60.0,
            "confidence": 0.8,
            "reason": "test",
            "available": True,
            "metadata": {},
        }
        for pillar in FUSION_PILLARS
    }
    breadth = {
        "key": "breadth",
        "score": 99.0,
        "confidence": 0.9,
        "reason": "diagnostic",
        "available": True,
        "metadata": {},
    }
    without = fuse_titan_score(components)
    with_b = fuse_titan_score(components, breadth=breadth)
    assert without["titan_score"] == with_b["titan_score"]
    assert with_b["breadth_score"] == 99.0


def test_fusion_disabled_skips_audit_stamp(monkeypatch):
    from titan_fusion import apply_fusion_to_audit, fusion_enabled

    monkeypatch.setenv("TITAN_FUSION_ENABLED", "0")
    assert fusion_enabled() is False
    audit = {"effective_intent_score": 70.0}
    assert apply_fusion_to_audit(audit) is None
    assert "titan_fusion" not in audit
