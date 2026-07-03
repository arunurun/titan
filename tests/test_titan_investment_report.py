"""Unit tests for titan_investment_report."""

from __future__ import annotations


def _sample_audit() -> dict:
    return {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "effective_intent_score": 72.0,
        "z_score": 1.2,
        "atr_14_pct": 2.5,
        "next_week_score": 68.0,
        "next_day_score": 55.0,
        "action_signal": "accumulate",
        "sell_signal_risk_score": 4.2,
        "sell_signal_reasons": ["constructive trend"],
        "sector": "energy",
        "market_regime": {"regime": "BULL", "streak": 3},
        "market_breadth": {"n_symbols": 120, "pct_above_ema200": 58.0},
        "institutional_flow": {
            "available": True,
            "score": 65.0,
            "reasons": ["CMF20 positive"],
        },
        "fundamental_status": "adequate",
        "fundamental_score": 62.0,
        "factor_scores": {
            "technical": {"available": True, "score": 72.0, "confidence": 0.85, "reasons": ["intent"]},
            "fundamentals": {"available": True, "score": 62.0, "confidence": 0.7, "reasons": ["adequate"]},
            "institutional_flow": {"available": True, "score": 65.0, "confidence": 0.8, "reasons": ["CMF"]},
            "sector_strength": {"available": True, "score": 55.0, "confidence": 0.75, "reasons": ["mid pack"]},
            "market_regime": {"available": True, "score": 75.0, "confidence": 0.8, "reasons": ["BULL"]},
            "risk": {"available": True, "score": 40.0, "confidence": 0.7, "reasons": ["moderate"]},
        },
        "titan_fusion": {
            "titan_score": 66.5,
            "overall_confidence": 0.82,
            "technical_score": 72.0,
            "regime_score": 75.0,
            "overall_explanation": "Technical\n72\n×\n30%\n=\n21.6",
        },
    }


def test_generate_investment_report_structure():
    from titan_investment_report import generate_investment_report

    report = generate_investment_report(_sample_audit())
    assert report["symbol"] == "RELIANCE"
    assert report["action_signal"] == "accumulate"
    assert report["titan_score"] == 66.5
    assert "technical_summary" in report["sections"]
    assert "final_decision" in report["sections"]
    assert "ACCUMULATE" in report["sections"]["final_decision"][0]
    assert "outlook" in report
    assert report["outlook"]["next_week_outlook"] == "cautiously positive"
    assert report["markdown"].startswith("# Titan Investment Report")


def test_generate_investment_report_graceful_without_fusion():
    from titan_investment_report import generate_investment_report

    audit = {"symbol": "TEST", "action_signal": "hold", "effective_intent_score": 50.0}
    report = generate_investment_report(audit)
    assert report["action_signal"] == "hold"
    assert "Fusion" not in report["markdown"]
    assert report["titan_score"] is None


def test_digest_factor_lines_graceful_without_fusion():
    from sector_audit import _format_fusion_factor_digest_lines

    assert _format_fusion_factor_digest_lines({}) == []
    assert _format_fusion_factor_digest_lines({"titan_fusion": None}) == []
