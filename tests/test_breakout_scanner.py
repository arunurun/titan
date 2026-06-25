from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_scanner import serialize_candidate, _build_report_markdown  # noqa: E402


def test_serialize_candidate_maps_api_fields():
    row = {
        "Ticker": "ABC",
        "Tier": "Small-Cap (Nifty Smallcap 100)",
        "Price": 100.0,
        "Change": "+5.25%",
        "Volume Mult": "3.5x",
        "RSI": 55.0,
        "ADX": 28.0,
        "Entry Range": "98.5 - 101.0",
        "Est. Stop-Loss": 95.0,
        "Est. Target (1:2)": 110.0,
        "Est. Gain": "10.0%",
        "Risk Flags": "HIGH CIRCUIT RISK",
        "Payload": "payload text",
    }
    out = serialize_candidate(row)
    assert out["ticker"] == "ABC"
    assert out["change_pct"] == 5.25
    assert out["volume_mult"] == 3.5
    assert out["est_gain_pct"] == 10.0
    assert out["payload"] == "payload text"


def test_build_report_markdown_empty():
    import datetime

    md = _build_report_markdown([], datetime.date(2026, 6, 25))
    assert "No small-cap or micro-cap stocks met" in md


@pytest.fixture
def flask_client():
    control_ui = ROOT / "control_ui"
    if str(control_ui) not in sys.path:
        sys.path.insert(0, str(control_ui))
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))

    from app import app  # noqa: E402

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_api_breakouts_endpoint_returns_json(monkeypatch, flask_client):
    sample = {
        "ok": True,
        "scan_date": "2026-06-25",
        "started_at": "2026-06-25T10:00:00",
        "finished_at": "2026-06-25T10:00:01",
        "duration_sec": 1.0,
        "tickers_scanned": 2,
        "tier_ticker_counts": {"Small-Cap (Nifty Smallcap 100)": 1, "Micro-Cap (Nifty Microcap 250)": 1},
        "candidate_count": 1,
        "tier_candidate_counts": {
            "Small-Cap (Nifty Smallcap 100)": 1,
            "Micro-Cap (Nifty Microcap 250)": 0,
        },
        "candidates": [{"ticker": "ABC", "tier": "Small-Cap (Nifty Smallcap 100)"}],
        "report_path": "output/breakouts/daily_breakout_report_v2.md",
        "log_path": "output/breakouts/breakout_scanner_run.log",
        "report_markdown": "# report",
    }

    def _fake_run(**kwargs):
        assert kwargs["emit_to_stdout"] is False
        assert kwargs["write_report"] is True
        return dict(sample)

    monkeypatch.setattr("app.run_breakout_scan", _fake_run)

    resp = flask_client.post(
        "/api/breakouts",
        data=json.dumps({"write_report": True, "include_report_markdown": False}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["candidate_count"] == 1
    assert "report_markdown" not in body


def test_api_breakouts_endpoint_handles_errors(monkeypatch, flask_client):
    def _boom(**kwargs):
        raise RuntimeError("scan failed")

    monkeypatch.setattr("app.run_breakout_scan", _boom)

    resp = flask_client.post("/api/breakouts")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert "scan failed" in body["error"]
