"""Smoke tests for Phase 3 feature_calibration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _synthetic_rows(n: int = 40) -> list[dict]:
    rows: list[dict] = []
    for i in range(n):
        tech = 45.0 + (i % 10) * 3.0
        rel = 40.0 + (i % 7) * 5.0
        fund = 50.0 + (i % 5)
        flow = 48.0 + (i % 6)
        regime = 72.0 if i % 2 == 0 else 58.0
        risk = 80.0 - (i % 4) * 2.0
        up = tech > 55 and rel > 50
        rows.append(
            {
                "factor_scores": {
                    "technical": {"score": tech, "available": True},
                    "relative_strength": {"score": rel, "available": True},
                    "institutional_flow": {"score": flow, "available": True},
                    "fundamentals": {"score": fund, "available": True},
                    "market_regime": {"score": regime, "available": True},
                    "sector_strength": {"score": rel, "available": True},
                    "risk": {"score": risk, "available": True},
                },
                "forward_return_5d_up": up,
            }
        )
    return rows


def test_suggest_titan_weights_correlation():
    from feature_calibration import suggest_titan_weights
    from titan_fusion import FUSION_PILLARS

    report = suggest_titan_weights(_synthetic_rows(), method="correlation")
    weights = report["weights"]
    assert report["method"] == "correlation"
    assert report["n_rows"] >= 10
    assert pytest.approx(sum(weights[p] for p in FUSION_PILLARS), abs=1e-5) == 1.0


def test_fit_feature_importance_alias():
    from feature_calibration import fit_feature_importance, suggest_titan_weights

    rows = _synthetic_rows()
    a = fit_feature_importance(rows, method="correlation")
    b = suggest_titan_weights(rows, method="correlation")
    assert a["weights"] == b["weights"]
    assert a["method"] == b["method"]


def test_insufficient_rows_returns_defaults():
    from feature_calibration import suggest_titan_weights
    from titan_fusion import DEFAULT_FUSION_WEIGHTS

    report = suggest_titan_weights(_synthetic_rows(3), method="correlation")
    assert report["method"] == "default"
    assert report["warning"]
    assert report["weights"] == DEFAULT_FUSION_WEIGHTS


def test_write_recommended_weights(tmp_path: Path):
    from feature_calibration import suggest_titan_weights, write_recommended_weights

    report = suggest_titan_weights(_synthetic_rows(), method="correlation")
    out = write_recommended_weights(report, tmp_path / "recommended_weights.json")
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "weights" in payload
    assert payload["method"] == "correlation"
