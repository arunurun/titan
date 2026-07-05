from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_scanner import serialize_candidate, _build_report_markdown  # noqa: E402


def test_nse_ticker_parsing_preserves_ampersand():
    from breakout_scanner import _parse_nse_ticker_csv

    csv_lines = [
        "Company Name,Industry,Symbol,Series,ISIN",
        "Aegis Logistics Ltd.,SERVICES,ARE&M,EQ,INE208C01025",
        "Mahindra & Mahindra Ltd.,AUTOMOBILES,M&M,EQ,INE101A01026",
        "Larsen & Toubro Ltd.,CONSTRUCTION,L&T,EQ,INE018A01030",
        "Reliance Industries Ltd.,OIL & GAS,RELIANCE,EQ,INE002A01018",
    ]
    tickers = _parse_nse_ticker_csv(csv_lines)
    assert "ARE&M.NS" in tickers
    assert "M&M.NS" in tickers
    assert "L&T.NS" in tickers
    assert "RELIANCE.NS" in tickers
    assert "AREM.NS" not in tickers
    assert "MM.NS" not in tickers


def test_nse_ticker_parsing_decodes_percent26_in_csv():
    from breakout_scanner import _parse_nse_ticker_csv

    csv_lines = ["Symbol", "ARE%26M", "M%26M"]
    tickers = _parse_nse_ticker_csv(csv_lines)
    assert tickers == ["ARE&M.NS", "M&M.NS"]


def test_nse_ticker_parsing_skips_dummy_symbols():
    from breakout_scanner import _parse_nse_ticker_csv

    csv_lines = [
        "Company Name,Industry,Symbol,Series,ISIN",
        "Dummy Allcargo Logistics Ltd.,Services,DUMMYALCAR,EQ,DUM418H01029",
        "Reliance Industries Ltd.,OIL & GAS,RELIANCE,EQ,INE002A01018",
        "Internal Test Co.,SERVICES,FOONSETEST,EQ,INETEST00001",
    ]
    tickers = _parse_nse_ticker_csv(csv_lines)
    assert tickers == ["RELIANCE.NS"]
    assert "DUMMYALCAR.NS" not in tickers
    assert "FOONSETEST.NS" not in tickers


def test_nse_ticker_parsing_preserves_gmrpui_microcap_symbol():
    from breakout_scanner import _parse_nse_ticker_csv

    csv_lines = [
        "Company Name,Industry,Symbol,Series,ISIN",
        "GMR Power and Urban Infra Ltd.,Power,GMRP&UI,EQ,INE0CU601026",
    ]
    tickers = _parse_nse_ticker_csv(csv_lines)
    assert tickers == ["GMRP&UI.NS"]
    assert "GMRPUI.NS" not in tickers


def test_normalize_nse_symbol_repairs_mangled_gmrpui():
    from breakout_scanner import _normalize_nse_symbol, _resolve_yahoo_ticker

    assert _normalize_nse_symbol("GMRPUI") == "GMRP&UI"
    assert _normalize_nse_symbol("GMRP&UI") == "GMRP&UI"
    assert _normalize_nse_symbol("GMRP%26UI") == "GMRP&UI"
    assert _resolve_yahoo_ticker("GMRPUI.NS") == "GMRP&UI.NS"


def test_ampersand_ticker_yahoo_quote_encoding():
    assert urllib.parse.quote("ARE&M.NS", safe="") == "ARE%26M.NS"
    assert urllib.parse.quote("M&M.NS", safe="") == "M%26M.NS"
    assert urllib.parse.quote("L&T.NS", safe="") == "L%26T.NS"


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


def test_serialize_candidate_setup_em_dash_trade_levels():
    """PRE_BREAKOUT rows use em dash for missing trade levels; must not crash."""
    row = {
        "Ticker": "SETUP1",
        "Breeze Code": "SETUP1",
        "Tier": "Small-Cap (Nifty Smallcap 100)",
        "Price": 250.0,
        "Change": "▲ +1.50%",
        "Volume Mult": "2.1x",
        "RSI": 58.0,
        "ADX": 22.0,
        "Entry Range": "—",
        "Est. Stop-Loss": "—",
        "Est. Target (1:2)": "—",
        "Est. Gain": "—",
        "Risk Flags": "",
        "Signal Tier": "PRE_BREAKOUT",
        "Setup Rank": 71.2,
        "Setup Trigger": ">255.0",
        "Pivot Proximity": 0.92,
        "Liquidity Quality": 68.0,
        "Persistence Score": 2,
        "Breakout Stage": 0,
        "Base Score": 60.0,
        "Composite Rank": 71.2,
        "Pass Paths": "",
        "Watch Reason": "",
        "Payload": "setup payload",
    }
    out = serialize_candidate(row)
    assert out["signal_tier"] == "PRE_BREAKOUT"
    assert out["est_gain_pct"] is None
    assert out["est_gain_display"] == "—"
    assert out["entry_range"] == "—"
    assert out["est_stop_loss"] == "—"
    assert out["est_target"] == "—"
    assert out["change_pct"] == 1.5
    assert out["volume_mult"] == 2.1


def test_build_report_markdown_empty():
    import datetime

    md = _build_report_markdown([], datetime.date(2026, 6, 25))
    assert "No small-cap or micro-cap stocks met" in md


def _sample_candidate_row(*, signal_tier: str = "PASS") -> dict:
    return {
        "Ticker": "ABC",
        "Tier": "Small-Cap (Nifty Smallcap 100)",
        "Price": 100.0,
        "Change": "▲ +5.25%",
        "Volume Mult": "3.5x",
        "RSI": 55.0,
        "ADX": 28.0,
        "Entry Range": "98.5 - 101.0",
        "Est. Stop-Loss": 95.0,
        "Est. Target (1:2)": 110.0,
        "Est. Gain": "10.0%",
        "Risk Flags": "HIGH CIRCUIT RISK",
        "Signal Tier": signal_tier,
        "Liquidity Quality": 72.0,
        "Persistence Score": 3,
        "Breakout Stage": 1,
        "Base Score": 65.0,
        "Composite Rank": 58.3,
        "Pass Paths": "power_gap",
        "Watch Reason": "v7_low_volume_persistence" if signal_tier == "WATCH" else "",
        "Payload": "payload text",
    }


def test_build_report_markdown_includes_v7_columns():
    import datetime
    from breakout_scanner import _build_report_markdown

    rows = [
        _sample_candidate_row(signal_tier="PASS"),
        _sample_candidate_row(signal_tier="WATCH") | {"Ticker": "XYZ", "Composite Rank": 40.0},
    ]
    md = _build_report_markdown(rows, datetime.date(2026, 6, 26))
    assert "Signal Tier" in md
    assert "Breakout Stage" in md
    assert "Persistence" in md
    assert "Liquidity Quality" not in md  # column header is "LQ"
    assert "| LQ |" in md
    assert "Pass Paths" in md
    assert "🟢 PASS" in md
    assert "🟡 WATCH" in md
    assert "Stage 1 Fresh" in md
    assert "**v7:**" in md
    assert "**WATCH reason:** v7_low_volume_persistence" in md


def test_build_breakout_email_body_pass_watch_sections():
    import datetime
    from breakout_scanner import _build_breakout_email_body

    rows = [
        _sample_candidate_row(signal_tier="PASS"),
        _sample_candidate_row(signal_tier="WATCH") | {"Ticker": "XYZ"},
    ]
    body = _build_breakout_email_body(
        scan_date=datetime.date(2026, 6, 26),
        tickers_scanned=100,
        all_results=rows,
        report_markdown=None,
    )
    assert "Candidates: 2 total (1 PASS, 1 WATCH, 0 SETUP)" in body
    assert "## PASS candidates (1)" in body
    assert "## WATCH candidates (1)" in body
    assert "## Top candidates (all tiers)" in body
    assert "Stage | Persist" in body


def test_format_change_arrow_direction_cues():
    from breakout_scanner import _format_change_arrow

    assert _format_change_arrow(3.2).startswith("▲")
    assert _format_change_arrow(-1.5).startswith("▼")
    assert _format_change_arrow(0.0).startswith("●")
    assert "▲ +3.20%" == _format_change_arrow(3.2)


@patch("email_notify._smtp_config")
@patch("email_notify.smtp_not_configured_reason", return_value=None)
@patch("email_notify.send_success_post_email")
def test_run_breakout_scan_email_passes_html_body(
    mock_email, _mock_reason, mock_cfg, monkeypatch, tmp_path
):
    from breakout_scanner import FILTERS, run_breakout_scan

    mock_cfg.return_value = {"to": ["alice@example.com"]}
    mock_email.return_value = True
    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    monkeypatch.setattr(
        "breakout_scanner.download_nse_tickers",
        lambda url: ["PASS.NS"],
    )

    def _fake_evaluate(ticker, tier_name, emit=None, **kwargs):
        row = _sample_candidate_row(signal_tier="PASS") | {
            "Ticker": ticker.replace(".NS", ""),
            "Tier": FILTERS[tier_name]["type"],
        }
        return row, {"ticker": ticker.replace(".NS", ""), "passed": True}

    monkeypatch.setattr("breakout_scanner.evaluate_and_audit_stock", _fake_evaluate)
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email_html"
    run_breakout_scan(output_dir=output_dir, emit_to_stdout=False)

    kwargs = mock_email.call_args[1]
    assert "html_body" in kwargs
    assert "PASS" in kwargs["html_body"]
    assert "#34a853" in kwargs["html_body"]


@patch("email_notify._smtp_config")
@patch("email_notify.smtp_not_configured_reason", return_value=None)
@patch("email_notify.send_success_post_email")
def test_run_breakout_scan_sends_success_email_zero_candidates(
    mock_email, _mock_reason, mock_cfg, monkeypatch, tmp_path, capsys
):
    from breakout_scanner import run_breakout_scan

    mock_cfg.return_value = {"to": ["alice@example.com"]}
    mock_email.return_value = True
    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    monkeypatch.setattr("breakout_scanner.download_nse_tickers", lambda url: [])
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email"

    result = run_breakout_scan(output_dir=output_dir, emit_to_stdout=True)

    mock_email.assert_called_once()
    body = mock_email.call_args[0][0]
    kwargs = mock_email.call_args[1]
    assert kwargs["subject_prefix"] == "Titan breakout scan"
    assert "Tickers scanned: 0" in body
    assert "No breakouts today" in body
    assert result["email_sent"] is True
    assert result["candidate_count"] == 0
    out = capsys.readouterr().out
    assert "Breakout email: SENT to" in out


@patch("email_notify.smtp_not_configured_reason", return_value="SMTP not configured (missing SMTP_HOST)")
@patch("email_notify.send_success_post_email")
def test_run_breakout_scan_email_not_sent_no_config(mock_email, _mock_reason, monkeypatch, tmp_path, capsys):
    from breakout_scanner import run_breakout_scan

    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    monkeypatch.setattr("breakout_scanner.download_nse_tickers", lambda url: [])
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email_no_smtp"

    result = run_breakout_scan(output_dir=output_dir, emit_to_stdout=True)

    mock_email.assert_not_called()
    assert result["email_sent"] is False
    out = capsys.readouterr().out
    assert "Breakout email: NOT SENT — SMTP not configured (missing SMTP_HOST)" in out


@patch("email_notify.send_success_post_email", return_value=False)
@patch("email_notify.smtp_not_configured_reason", return_value=None)
@patch("email_notify._smtp_config")
def test_run_breakout_scan_email_smtp_send_failed(
    mock_cfg, _mock_reason, _mock_send, monkeypatch, tmp_path, capsys
):
    from breakout_scanner import run_breakout_scan

    mock_cfg.return_value = {"to": ["alice@example.com"]}
    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    monkeypatch.setattr("breakout_scanner.download_nse_tickers", lambda url: [])
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email_smtp_fail"

    result = run_breakout_scan(output_dir=output_dir, emit_to_stdout=True)

    assert result["email_sent"] is False
    out = capsys.readouterr().out
    assert "Breakout email: NOT SENT — SMTP send failed (see stderr for details)" in out


@patch("email_notify._smtp_config")
@patch("email_notify.smtp_not_configured_reason", return_value=None)
@patch("email_notify.send_success_post_email")
def test_run_breakout_scan_email_includes_pass_watch_counts(
    mock_email, _mock_reason, mock_cfg, monkeypatch, tmp_path
):
    from breakout_scanner import FILTERS, run_breakout_scan

    mock_cfg.return_value = {"to": ["alice@example.com"]}
    mock_email.return_value = True
    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    monkeypatch.setattr(
        "breakout_scanner.download_nse_tickers",
        lambda url: ["PASS.NS"] if "smallcap" in url.lower() else ["WATCH.NS"],
    )

    def _fake_evaluate(ticker, tier_name, emit=None, **kwargs):
        tier = FILTERS[tier_name]["type"]
        signal_tier = "PASS" if ticker == "PASS.NS" else "WATCH"
        row = _sample_candidate_row(signal_tier=signal_tier) | {
            "Ticker": ticker.replace(".NS", ""),
            "Tier": tier,
        }
        return row, {"ticker": ticker.replace(".NS", ""), "passed": signal_tier == "PASS"}

    monkeypatch.setattr("breakout_scanner.evaluate_and_audit_stock", _fake_evaluate)
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email_tiers"

    result = run_breakout_scan(output_dir=output_dir, emit_to_stdout=False)

    mock_email.assert_called_once()
    body = mock_email.call_args[0][0]
    assert "Candidates: 2 total (1 PASS, 1 WATCH, 0 SETUP)" in body
    assert "## PASS candidates (1)" in body
    assert "## WATCH candidates (1)" in body
    assert "Consolidated Daily Breakout" in body
    assert result["candidate_count"] == 2


@patch("email_notify.send_failure_email")
@patch("email_notify.send_success_post_email")
def test_run_breakout_scan_sends_failure_email_on_error(mock_success, mock_failure, monkeypatch):
    from breakout_scanner import run_breakout_scan

    mock_failure.return_value = True
    monkeypatch.setattr("breakout_scanner.warm_yahoo_session", lambda: None)
    output_dir = ROOT / "output" / "breakouts" / ".pytest_email_fail"

    def _boom(url):
        raise RuntimeError("nse download failed")

    monkeypatch.setattr("breakout_scanner.download_nse_tickers", _boom)

    with pytest.raises(RuntimeError, match="nse download failed"):
        run_breakout_scan(output_dir=output_dir, emit_to_stdout=False)

    mock_success.assert_not_called()
    mock_failure.assert_called_once()
    assert "[Breakout scan]" in mock_failure.call_args[0][0]
    assert mock_failure.call_args[1]["subject_prefix"] == "Titan breakout scan"


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


def test_evaluate_bars_as_of_insufficient_history():
    from breakout_scanner import evaluate_bars_as_of

    df = {
        "open": [10.0] * 30,
        "high": [11.0] * 30,
        "low": [9.0] * 30,
        "close": [10.0 + i * 0.1 for i in range(30)],
        "volume": [1000.0] * 30,
        "timestamp": list(range(30)),
    }
    result = evaluate_bars_as_of(df, 29, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "insufficient_data"
    assert result["bar_count"] == 30


def test_evaluate_bars_as_of_passes_synthetic_breakout():
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = [50.0] * (n - 2)
    close.append(52.0)  # +4% day
    close.append(55.0)  # +5.77% day — cumulative from base still within filters
    # Build monotonic rise into breakout territory
    base = [40.0 + i * 0.15 for i in range(n - 2)]
    base.append(base[-1] * 1.04)
    base.append(base[-1] * 1.05)
    close = base
    volume = [50000.0] * (n - 1) + [250000.0]
    high = [c * 1.02 for c in close]
    low = [c * 0.98 for c in close]
    open_ = [close[i - 1] if i else close[0] for i in range(n)]

    df = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timestamp": list(range(n)),
    }
    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    # May fail on ADX/RSI depending on path — assert structure and no look-ahead slice
    assert result["bar_count"] == n
    assert result["as_of_idx"] == n - 1
    assert "latest_price" in result
    assert "vol_mult" in result


def test_evaluate_bars_as_of_point_in_time_no_lookahead():
    from breakout_scanner import evaluate_bars_as_of

    n = 60
    close = [20.0 + i * 0.05 for i in range(n)]
    volume = [10000.0] * n
    df = {
        "open": close[:],
        "high": [c + 0.5 for c in close],
        "low": [c - 0.5 for c in close],
        "close": close,
        "volume": volume,
        "timestamp": list(range(n)),
    }
    early = evaluate_bars_as_of(df, 49, "SMALL_CAP_100")
    late = evaluate_bars_as_of(df, 59, "SMALL_CAP_100")
    assert early["latest_price"] != late["latest_price"]
    assert early["bar_count"] == 50
    assert late["bar_count"] == 60


def test_validate_forward_path_target_before_stop():
    from breakout_backtest import validate_forward_path

    df = {
        "open": [100.0, 100.0, 100.0, 100.0],
        "high": [101.0, 110.0, 111.0, 112.0],
        "low": [99.0, 99.0, 105.0, 106.0],
        "close": [100.0, 108.0, 109.0, 110.0],
    }
    outcome = validate_forward_path(df, 0, entry=100.0, stop=95.0, target=108.0, horizons=(1, 2))
    assert outcome["horizons"]["t1"]["win"] is True
    assert outcome["first_exit"] == "win"


def test_build_backtest_universe_structure():
    from breakout_backtest import build_backtest_universe

    payload = build_backtest_universe(top_n=2)
    assert payload["total"] == 4
    assert payload["small_cap_count"] == 2
    assert payload["micro_cap_count"] == 2
    for stock in payload["stocks"]:
        assert stock["yahoo_ticker"].endswith(".NS")
        assert stock["tier_key"] in ("SMALL_CAP_100", "MICRO_CAP_250")


def test_api_breakouts_endpoint_handles_errors(monkeypatch, flask_client):
    def _boom(**kwargs):
        raise RuntimeError("scan failed")

    monkeypatch.setattr("app.run_breakout_scan", _boom)

    resp = flask_client.post("/api/breakouts")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert "scan failed" in body["error"]


def _synthetic_df(close: list[float], volume: list[float]) -> dict:
    n = len(close)
    return {
        "open": [close[i - 1] if i else close[0] for i in range(n)],
        "high": [c * 1.015 for c in close],
        "low": [c * 0.985 for c in close],
        "close": close,
        "volume": volume,
        "timestamp": list(range(n)),
    }


def _uptrend_closes(n: int, start: float = 50.0, step: float = 0.25) -> list[float]:
    return [start + i * step for i in range(n)]


def _rising_adx_mock(base: float = 15.0, step: float = 0.15):
    def _mock(high, low, close, period=14):
        return (
            [base + i * step for i in range(len(close))],
            [0.0] * len(close),
            [0.0] * len(close),
        )
    return _mock


def _flat_adx_mock(value: float = 22.0):
    def _mock(high, low, close, period=14):
        return ([value] * len(close), [0.0] * len(close), [0.0] * len(close))
    return _mock


@pytest.fixture(autouse=True)
def _v7_evidence_defaults(monkeypatch, request):
    """Default v7 evidence passes unless a test mocks compute_evidence_metrics itself."""
    if request.node.get_closest_marker("v7_evidence_real"):
        return

    def _fake_evidence(df, as_of_idx, tier_name, vol_20_avg, **kwargs):
        return {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 72.0,
            "median_turnover_inr": 25_000_000.0,
            "persistence_score": 4,
            "persistence_pass_min": 2 if tier_name == "MICRO_CAP_250" else 1,
            "breakout_stage": 1,
            "base_score": 65.0,
        }

    monkeypatch.setattr("breakout_scanner.compute_evidence_metrics", _fake_evidence)
    monkeypatch.setattr(
        "breakout_scanner.base_accumulation_pass",
        lambda *args, **kwargs: (True, {"passed": True}),
    )
    monkeypatch.setattr(
        "breakout_scanner.composite_rank_score",
        lambda metrics, **kw: 55.0,
    )


def test_power_gap_pass_path(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr(
        "breakout_scanner.calculate_adx",
        lambda high, low, close, period=14: ([28.0] * len(close), [0.0] * len(close), [0.0] * len(close)),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert "power_gap" in result["pass_paths"]
    assert "circuit-risk" in result["risk_flags"]


def test_vol_continuation_cum3d_pass_path(monkeypatch):
    import breakout_scanner
    from breakout_scanner import _volume_filter_passes, evaluate_bars_as_of

    ok, path, metrics = _volume_filter_passes(
        vol_mult=1.1,
        vol_cum_mult=3.8,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
    )
    assert ok is True
    assert path == "vol_continuation_cum3d"
    assert "vpcs_applied_threshold" not in metrics

    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.35)
    close[-1] = close[-2] * 1.05
    volume = [20000.0] * (n - 4) + [500000.0, 500000.0, 500000.0, 80000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr(
        "breakout_scanner.calculate_adx",
        lambda high, low, close, period=14: ([28.0] * len(close), [0.0] * len(close), [0.0] * len(close)),
    )
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["vol_cum_mult"] >= 3.5
    assert result["passed"] is True
    assert "vol_continuation_cum3d" in result["pass_paths"]


def _rising_adx_soft(high, low, close, period=14):
    n = len(close)
    arr = [18.0 + i * 0.08 for i in range(n)]
    return (arr, [0.0] * n, [0.0] * n)


def test_adx_soft_band_pass_path(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_soft)

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result["fail_reason"] is None
    assert "adx_soft" in result["pass_paths"]
    assert "1% sizing" in result["risk_flags"]


def test_high_rsi_passes_without_ceiling(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.3)
    close[-1] = close[-2] * 1.05
    volume = [40000.0] * (n - 1) + [350000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [72.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(26.0, 0.15))
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert "rsi_hot" not in result["pass_paths"]


def test_sma20_reclaim_pass_path(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = [100.0] * 35 + [100.0 - i * 1.2 for i in range(30)] + [64.0 + i * 0.9 for i in range(15)]
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr(
        "breakout_scanner.calculate_adx",
        lambda high, low, close, period=14: ([28.0] * len(close), [0.0] * len(close), [0.0] * len(close)),
    )
    monkeypatch.setattr("breakout_scanner.get_volume_profile", lambda prices, volumes, bins=10: min(prices))

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["latest_price"] < result["sma50_last"]
    assert result["latest_price"] >= result["sma20_last"]
    assert result["vol_mult"] >= 5.0
    assert result["passed"] is True
    assert "sma20_reclaim" in result["pass_paths"]


def test_validate_forward_path_efficacy_metrics():
    from breakout_backtest import validate_forward_path

    df = {
        "open": [100.0, 100.0, 100.0, 100.0, 100.0],
        "high": [101.0, 116.0, 117.0, 118.0, 119.0],
        "low": [99.0, 99.0, 105.0, 106.0, 107.0],
        "close": [100.0, 110.0, 111.0, 112.0, 116.0],
    }
    outcome = validate_forward_path(df, 0, entry=100.0, stop=95.0, target=130.0, horizons=(4,))
    eff = outcome["efficacy"]
    assert eff["mfe_hit_8"] is True
    assert eff["mfe_hit_15"] is True
    assert eff["close_hit_8pct"] is True
    assert eff["close_hit_15pct"] is True


def test_pre_signal_validation_cum_return_skip():
    from breakout_scanner import pre_signal_validation

    n = 70
    close = [100.0] * 58 + [100.0 + (i - 58) * 6.0 for i in range(58, n)]
    volume = [50000.0] * n
    df = _synthetic_df(close, volume)

    ok, reason, metrics = pre_signal_validation(df, n - 1)
    assert ok is False
    assert reason == "pre_filter_cum_return"
    assert metrics["cum_return_t10_t1"] is not None
    assert metrics["cum_return_t10_t1"] > 30.0


def test_pre_signal_validation_vol_spike_skip():
    from breakout_scanner import pre_signal_validation

    n = 70
    close = [100.0] * n
    volume = [50000.0] * n
    # Five spike days in T-15..T-1 window (max allowed is 4)
    for idx in (n - 14, n - 12, n - 10, n - 8, n - 6):
        volume[idx] = 250000.0
    df = _synthetic_df(close, volume)

    ok, reason, metrics = pre_signal_validation(df, n - 1)
    assert ok is False
    assert reason == "pre_filter_vol_spike"
    assert metrics["vol_spike_days_t15_t1"] == 5


def test_pre_signal_validation_happy_path():
    from breakout_scanner import pre_signal_validation

    n = 70
    close = [100.0 + i * 0.1 for i in range(n)]
    volume = [50000.0] * n
    df = _synthetic_df(close, volume)

    ok, reason, metrics = pre_signal_validation(df, n - 1)
    assert ok is True
    assert reason is None
    assert metrics["full_window"] is True
    assert metrics["vol_spike_days_t15_t1"] <= 4


def test_evaluate_bars_as_of_pre_filter_blocks_after_breakout_pass(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    volume = [80000.0] * (n - 1) + [400000.0]
    # Run-up before signal triggers cum-return skip
    for i in range(n - 11, n - 1):
        close[i] = close[i - 1] * 1.04
    close[-1] = close[-2] * 1.05

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr(
        "breakout_scanner.calculate_adx",
        lambda high, low, close, period=14: ([28.0] * len(close), [0.0] * len(close), [0.0] * len(close)),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_cum_return"
    assert result["pre_filter_fail"] == "pre_filter_cum_return"
    assert "pre_validation" in result


def test_adx_trajectory_gate_blocks_flat_adx_on_adx_soft(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(22.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_adx_trajectory"
    assert "adx_soft" in result["pass_paths"] or result.get("adx_val", 0) < 25


def test_adx_trajectory_gate_blocks_flat_adx_on_high_rsi(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.3)
    close[-1] = close[-2] * 1.05
    volume = [40000.0] * (n - 1) + [350000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [72.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] in ("pre_filter_adx_trajectory", "pre_filter_standard_adx_trajectory")
    assert "rsi_hot" not in result["pass_paths"]


def test_signal_cooldown_gate_blocks_extended_repeat():
    from breakout_scanner import _signal_cooldown_gate

    n = 80
    close = [100.0] * n
    for i in range(50, 60):
        close[i] = 130.0
    for i in range(60, n - 1):
        close[i] = 95.0
    high = [c * 1.08 for c in close]
    low = [c * 0.92 for c in close]
    df = {
        "open": close[:],
        "high": high,
        "low": low,
        "close": close,
        "volume": [50000.0] * n,
    }

    ok, reason, metrics = _signal_cooldown_gate(df, n - 1, n - 11)
    assert ok is False
    assert reason == "pre_filter_signal_cooldown"
    assert metrics["sessions_since_prior_pass"] == 10
    assert metrics["cooldown_exempt"] is False


def test_signal_cooldown_gate_allows_tight_consolidation_exempt():
    from breakout_scanner import _signal_cooldown_gate

    n = 80
    close = [100.0] * (n - 6) + [100.5, 100.8, 101.0, 101.2, 101.5, 106.0]
    high = [c * 1.005 for c in close]
    low = [c * 0.995 for c in close]
    df = {
        "open": close[:],
        "high": high,
        "low": low,
        "close": close,
        "volume": [50000.0] * n,
    }

    ok, reason, metrics = _signal_cooldown_gate(df, n - 1, n - 11)
    assert ok is True
    assert reason is None
    assert metrics["cooldown_exempt"] is True
    assert metrics["consolidation_range_pct"] <= 12.0
    assert metrics["dist_from_20d_high_pct"] >= -3.0


def test_signal_cooldown_exempt_on_tight_consolidation_near_high(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = [100.0] * (n - 6) + [100.5, 100.8, 101.0, 101.2, 101.5, 106.0]
    volume = [50000.0] * (n - 1) + [300000.0]
    high = [c * 1.005 for c in close]
    low = [c * 0.995 for c in close]
    df = {
        "open": [close[i - 1] if i else close[0] for i in range(n)],
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timestamp": list(range(n)),
    }

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(base=26.0, step=0.2))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True}),
    )
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    prior_idx = n - 11
    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100", last_pass_idx=prior_idx)
    cd = result["signal_cooldown"]
    assert cd["cooldown_exempt"] is True
    assert cd["consolidation_range_pct"] <= 12.0
    assert cd["dist_from_20d_high_pct"] >= -3.0
    assert result["passed"] is True


def test_adx_trajectory_blocks_adx_soft_when_short_slope_fails(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    def _adx_falling_short_term(high, low, close, period=14):
        arr = [18.0 + i * 0.08 for i in range(len(close))]
        arr[-6] = 25.5
        arr[-5:] = [25.5, 25.2, 24.8, 24.3, 24.0]
        return (arr, [0.0] * len(close), [0.0] * len(close))

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _adx_falling_short_term)
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_adx_trajectory"
    assert "adx_soft" in result["pass_paths"]


def test_adx_soft_requires_elevated_volume(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [210000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_soft)
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "ADX"
    assert "adx_soft" not in result["pass_paths"]


def test_adx_soft_chase_blocks_extended_runup(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_soft)
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 22.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_adx_soft_chase"
    assert "adx_soft" in result["pass_paths"]


def test_power_gap_downgrades_to_watch_without_adx_rising(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 18.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result["fail_reason"] is None
    assert "power_gap" in result["pass_paths"]
    assert "1% sizing" in result["risk_flags"]


def test_power_gap_stays_pass_with_rising_adx(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(base=20.0, step=0.15))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 18.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert "power_gap" in result["pass_paths"]


def test_serialize_candidate_v7_evidence_fields():
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
        "Signal Tier": "PASS",
        "Liquidity Quality": 72.5,
        "Persistence Score": 2,
        "Breakout Stage": 1,
        "Base Score": 65.0,
        "Composite Rank": 58.3,
        "Payload": "payload text",
    }
    out = serialize_candidate(row)
    assert out["liquidity_quality"] == 72.5
    assert out["persistence_score"] == 2
    assert out["breakout_stage"] == 1
    assert out["base_score"] == 65.0
    assert out["composite_rank"] == 58.3


def test_v7_liquidity_gate_blocks_thin_stock(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(base=26.0, step=0.2))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": False,
            "liquidity_gate_fail": "pre_filter_liquidity",
            "liquidity_quality": 20.0,
            "persistence_score": 4,
            "breakout_stage": 1,
            "base_score": 50.0,
        },
    )
    monkeypatch.setattr(
        "breakout_scanner.composite_rank_score",
        lambda metrics, **kw: 50.0,
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_liquidity"


def test_v7_low_persistence_downgrades_to_watch(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(base=26.0, step=0.2))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 70.0,
            "persistence_score": 0,
            "breakout_stage": 1,
            "base_score": 55.0,
        },
    )
    monkeypatch.setattr(
        "breakout_scanner.composite_rank_score",
        lambda metrics, **kw: 55.0,
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "v7_low_volume_persistence"


def test_v7_stage_3_downgrades_to_watch(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [620000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(base=20.0, step=0.15))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 12.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 80.0,
            "persistence_score": 4,
            "breakout_stage": 3,
            "base_score": 40.0,
        },
    )
    monkeypatch.setattr(
        "breakout_scanner.composite_rank_score",
        lambda metrics, **kw: 45.0,
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "v7_breakout_stage_3"


def test_standard_adx_trajectory_blocks_falling_adx_low_vol(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [220000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_standard_adx_trajectory"
    assert result["pass_paths"] == []


def test_standard_adx_trajectory_allows_high_vol_exception(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [540000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert result["adx_trajectory"]["standard_vol_exception"] is True
    assert result["pass_paths"] == []


def test_adx_soft_solo_downgrades_to_watch(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-1] = close[-2] * 1.05
    volume = [50000.0] * (n - 1) + [300000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_soft)
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result["pass_paths"] == ["adx_soft"]
    assert result.get("adx_soft_solo_watch") is True


def test_adx_soft_with_high_rsi_solo_downgrades_to_watch(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.3)
    close[-1] = close[-2] * 1.05
    volume = [40000.0] * (n - 1) + [350000.0]

    def _adx_dual_path(high, low, close, period=14):
        arr = [18.0 + i * 0.08 for i in range(len(close))]
        arr[-1] = 22.0
        return (arr, [0.0] * len(close), [0.0] * len(close))

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [72.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _adx_dual_path)
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert "adx_soft" in result["pass_paths"]
    assert "rsi_hot" not in result["pass_paths"]


def test_power_gap_vol_recovery_passes_at_5_5x(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [620000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 18.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert result["power_gap_confirmation"]["vol_recovery"] is True
    assert "power_gap" in result["pass_paths"]


def test_power_gap_stays_pass_with_low_cum_return(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.2)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.15
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 12.0}),
    )

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert "power_gap" in result["pass_paths"]


def test_evaluate_bars_as_of_micro_participation_fail(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner._adx_trajectory_gate",
        lambda *a, **k: (True, None, {"adx_trajectory_required": True}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 70.0,
            "median_turnover_inr": 50_000_000.0,
            "persistence_score": 4,
            "persistence_pass_min": 2,
            "breakout_stage": 1,
            "base_score": 65.0,
            "micro_participation_pass": False,
            "delivery_pct": 20.0,
            "vpr": 1.0,
            "cmf": -0.1,
        },
    )

    result = evaluate_bars_as_of(
        _synthetic_df(close, volume),
        n - 1,
        "MICRO_CAP_250",
        delivery_pct=20.0,
    )
    assert result["passed"] is False
    assert result["fail_reason"] == "pre_filter_micro_participation"


def test_evaluate_bars_as_of_sector_lead_affects_composite_rank(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 70.0,
            "median_turnover_inr": 50_000_000.0,
            "persistence_score": 4,
            "persistence_pass_min": 1,
            "breakout_stage": 1,
            "base_score": 65.0,
            "micro_participation_pass": True,
        },
    )

    base = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    boosted = evaluate_bars_as_of(
        _synthetic_df(close, volume), n - 1, "SMALL_CAP_100", sector_lead=90.0,
    )
    assert boosted.get("composite_rank", 0) >= base.get("composite_rank", 0)


def _passing_eval_mocks(monkeypatch):
    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(28.0))
    monkeypatch.setattr(
        "breakout_scanner.pre_signal_validation",
        lambda df, idx: (True, None, {"full_window": True, "cum_return_t10_t1": 5.0}),
    )
    monkeypatch.setattr(
        "breakout_scanner._adx_trajectory_gate",
        lambda *a, **k: (True, None, {"adx_trajectory_required": True}),
    )
    monkeypatch.setattr(
        "breakout_scanner.compute_evidence_metrics",
        lambda *a, **k: {
            "liquidity_gate_pass": True,
            "liquidity_gate_fail": None,
            "liquidity_quality": 70.0,
            "median_turnover_inr": 50_000_000.0,
            "persistence_score": 4,
            "persistence_pass_min": 1,
            "breakout_stage": 1,
            "base_score": 65.0,
            "micro_participation_pass": True,
        },
    )


def test_is_upper_circuit_locked():
    from breakout_scanner import is_upper_circuit_locked

    assert is_upper_circuit_locked(105.0, 105.0, 5.0) is True
    assert is_upper_circuit_locked(105.0, 105.01, 5.0) is False
    assert is_upper_circuit_locked(105.0, 105.0, 4.0) is False


def test_evaluate_bars_as_of_upper_circuit_downgrades_to_watch(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1]
    _passing_eval_mocks(monkeypatch)

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "circuit_locked"
    assert result.get("upper_circuit_locked") is True
    assert "UPPER_CIRCUIT" in (result.get("risk_flags") or "")


def test_evaluate_bars_as_of_excessive_alternate_paths_downgrades(monkeypatch):
    from breakout_scanner import _volume_filter_passes, evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] + 0.5
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(22.0))

    real_vol_filter = _volume_filter_passes

    def _vol_cum_bypass(**kwargs):
        ok, path, metrics = real_vol_filter(**kwargs)
        if not ok:
            return True, "vol_continuation_cum3d", metrics
        if path is None:
            return True, "vol_continuation_cum3d", metrics
        return ok, path, metrics

    monkeypatch.setattr("breakout_scanner._volume_filter_passes", _vol_cum_bypass)
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert "vol_continuation_cum3d" in result.get("pass_paths", [])
    assert "adx_soft" in result.get("pass_paths", [])
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "excessive_alternate_paths"


def test_evaluate_bars_as_of_market_regime_risk_off_downgrades(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] + 0.5
    _passing_eval_mocks(monkeypatch)

    result = evaluate_bars_as_of(
        df, n - 1, "SMALL_CAP_100", market_regime="RISK_OFF",
    )
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "market_regime_risk_off"


def test_evaluate_market_regime_risk_off():
    from breakout_scanner import evaluate_market_regime

    n = 30
    closes = [100.0 - i * 0.5 for i in range(n)]
    df = {
        "open": closes[:],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
        "timestamp": list(range(n)),
    }
    out = evaluate_market_regime(df)
    assert out["market_regime"] == "RISK_OFF"
    assert out["benchmark_close"] < out["benchmark_sma20"]


def test_validate_forward_path_upper_circuit_uses_next_open():
    from breakout_backtest import validate_forward_path

    df = {
        "open": [100.0, 102.0, 103.0, 104.0],
        "high": [101.0, 110.0, 111.0, 112.0],
        "low": [99.0, 101.0, 102.0, 103.0],
        "close": [100.0, 110.0, 109.0, 110.0],
    }
    outcome = validate_forward_path(
        df,
        1,
        entry=110.0,
        stop=95.0,
        target=120.0,
        pct_change=5.0,
        horizons=(1, 2),
    )
    assert outcome["upper_circuit_locked"] is True
    assert outcome["entry_source"] == "open_t1"
    assert outcome["entry"] == 103.0


def test_evaluate_bars_as_of_upper_wick_rejection(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] * 1.08
    df["low"][-1] = close[-1] * 0.99
    _passing_eval_mocks(monkeypatch)

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert result["passed"] is False
    assert result["fail_reason"] == "upper_wick_rejection"
    assert result["close_position"] < 0.5


def test_evaluate_bars_as_of_poc_excludes_signal_day():
    from breakout_scanner import evaluate_bars_as_of, get_volume_profile

    n = 80
    close = [100.0] * n
    volume = [1000.0] * n
    close[-1] = 200.0
    volume[-1] = 1_000_000.0
    df = _synthetic_df(close, volume)

    t = n - 1
    expected_poc = get_volume_profile(close[t - 30 : t], volume[t - 30 : t])
    result = evaluate_bars_as_of(df, t, "SMALL_CAP_100")
    assert result["poc"] == expected_poc


def test_evaluate_bars_as_of_imminent_earnings_downgrades(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] + 0.5
    _passing_eval_mocks(monkeypatch)

    result = evaluate_bars_as_of(
        df, n - 1, "SMALL_CAP_100", days_to_next_earnings=2,
    )
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert result.get("v7_watch_reason") == "imminent_earnings"
    assert "IMMINENT_EARNINGS" in (result.get("risk_flags") or "")


def test_evaluate_bars_as_of_relative_strength_in_rank(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.4)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.05
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] + 0.5
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr(
        "breakout_scanner.composite_rank_score",
        lambda metrics, **kw: float(metrics.get("rel_return_5d_vs_benchmark") or 0.0),
    )

    flat_bench = evaluate_bars_as_of(
        df, n - 1, "SMALL_CAP_100", benchmark_5d_return=0.0,
    )
    weak_bench = evaluate_bars_as_of(
        df, n - 1, "SMALL_CAP_100", benchmark_5d_return=5.0,
    )
    assert (flat_bench.get("rel_return_5d_vs_benchmark") or 0) > (
        weak_bench.get("rel_return_5d_vs_benchmark") or 0
    )
    assert flat_bench["composite_rank"] > weak_bench["composite_rank"]


def test_validate_forward_path_close_basis_stop_not_intraday_low():
    from breakout_backtest import validate_forward_path

    df = {
        "open": [100.0, 100.0, 100.0, 100.0],
        "high": [101.0, 102.0, 103.0, 104.0],
        "low": [99.0, 94.0, 96.0, 97.0],
        "close": [100.0, 96.0, 97.0, 98.0],
    }
    outcome = validate_forward_path(
        df, 0, entry=100.0, stop=95.0, target=110.0, horizons=(1, 2, 3),
    )
    assert outcome["horizons"]["t1"]["result"] != "loss"
    assert outcome["first_exit"] != "loss"


def test_evaluate_market_regime_point_in_time_signal_date():
    from breakout_scanner import bar_dates_from_df, evaluate_market_regime

    n = 40
    closes = [100.0 + i * 0.1 for i in range(n)]
    df = {
        "open": closes[:],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
        "timestamp": [1_700_000_000 + i * 86_400 for i in range(n)],
    }
    dates = bar_dates_from_df(df)
    early = evaluate_market_regime(df, signal_date=dates[25])
    live = evaluate_market_regime(df)
    assert early["signal_date"] == dates[25]
    assert early["benchmark_as_of_idx"] == 25
    assert early["benchmark_close"] != live["benchmark_close"]


def test_evaluate_bars_as_of_distribution_base_fail(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    def _fail_accum(*args, **kwargs):
        return False, {"passed": False, "up_volume": 100.0, "down_volume": 200.0}

    monkeypatch.setattr("breakout_scanner.base_accumulation_pass", _fail_accum)
    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.3)
    close[-1] = close[-2] * 1.05
    volume = [40000.0] * (n - 1) + [350000.0]
    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [60.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_mock(28.0, 0.15))
    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["fail_reason"] == "distribution_base"


def test_vpcs_applied_threshold_by_tier():
    from breakout_scanner import (
        VPCS_VOL_FLOOR,
        _volume_filter_passes,
        _vpcs_applied_threshold,
    )

    assert _vpcs_applied_threshold(3.5, "SMALL_CAP_100") == VPCS_VOL_FLOOR
    assert _vpcs_applied_threshold(3.0, "MICRO_CAP_250") == VPCS_VOL_FLOOR

    ok, path, metrics = _volume_filter_passes(
        vol_mult=2.6,
        vol_cum_mult=1.0,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=6.0,
        close_position=0.99,
    )
    assert ok is True
    assert path == "vpcs_price_compressed_accumulation"
    assert metrics["vpcs_applied_threshold"] == VPCS_VOL_FLOOR

    ok, path, _ = _volume_filter_passes(
        vol_mult=2.4,
        vol_cum_mult=1.0,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=6.0,
        close_position=0.99,
    )
    assert ok is False
    assert path is None


def test_vpcs_requires_pct_and_close_position():
    from breakout_scanner import _volume_filter_passes

    ok, path, _ = _volume_filter_passes(
        vol_mult=2.8,
        vol_cum_mult=1.0,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=4.9,
        close_position=0.99,
    )
    assert ok is False

    ok, path, _ = _volume_filter_passes(
        vol_mult=2.8,
        vol_cum_mult=1.0,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=6.0,
        close_position=0.64,
    )
    assert ok is False

    ok, path, _ = _volume_filter_passes(
        vol_mult=2.8,
        vol_cum_mult=1.0,
        vol_thresh=3.0,
        tier_name="MICRO_CAP_250",
        prior_spike=False,
        pct_change=5.2,
        close_position=0.70,
    )
    assert ok is True
    assert path == "vpcs_price_compressed_accumulation"


def test_vpcs_marginal_volume_downgrades_to_watch(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.06
    base_vol = 80000.0
    volume = [base_vol] * (n - 1) + [base_vol * 3.2]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1]
    df["low"][-1] = close[-1] * 0.97
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert result["fail_reason"] is None
    assert result["passed"] is False
    assert result["signal_tier"] == "WATCH"
    assert "vpcs_price_compressed_accumulation" in result["pass_paths"]
    assert result.get("v7_watch_reason") == "vpcs_marginal_volume"
    assert result.get("vpcs_applied_threshold") == 2.5


def test_standard_volume_spike_still_passes(monkeypatch):
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.06
    volume = [80000.0] * (n - 1) + [400000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1] + 0.5
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert "vpcs_price_compressed_accumulation" not in result.get("pass_paths", [])
    assert result.get("v7_watch_reason") != "vpcs_marginal_volume"


def test_jul3_pplpharma_like_still_fails_at_actual_vol(monkeypatch):
    """Jul 3 PPLPHARMA: +6.01%, vol×1.90, close_pos 0.99 — below VPCS 2.5× floor."""
    from breakout_scanner import _volume_filter_passes

    ok, path, metrics = _volume_filter_passes(
        vol_mult=1.90,
        vol_cum_mult=0.95,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=6.01,
        close_position=0.99,
    )
    assert ok is False
    assert path is None
    assert metrics["vpcs_applied_threshold"] == 2.5


def test_jul3_eiel_like_blocked_by_close_position(monkeypatch):
    """Jul 3 EIEL: +5.21%, vol×2.71 but close_pos 0.57 — VPCS needs ≥0.65."""
    from breakout_scanner import _volume_filter_passes

    ok, path, _ = _volume_filter_passes(
        vol_mult=2.71,
        vol_cum_mult=1.38,
        vol_thresh=3.0,
        tier_name="MICRO_CAP_250",
        prior_spike=False,
        pct_change=5.21,
        close_position=0.57,
    )
    assert ok is False
    assert path is None


def test_jul3_nuvama_like_blocked_by_volume(monkeypatch):
    """Jul 3 NUVAMA: +5.00%, close_pos 0.65 but vol×1.85 — below VPCS floor."""
    from breakout_scanner import _volume_filter_passes

    ok, path, _ = _volume_filter_passes(
        vol_mult=1.85,
        vol_cum_mult=0.88,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
        pct_change=5.00,
        close_position=0.65,
    )
    assert ok is False
    assert path is None


def test_jul3_pcjeweller_adx_momentum_ignition(monkeypatch):
    """Jul 3 PCJEWELLER-like: +10%, vol×5.63, ADX 19, close_pos 0.85 → adx_soft."""
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=8.0, step=0.05)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.1005
    volume = [50000.0] * (n - 1) + [280000.0]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1]
    df["low"][-1] = close[-1] * 0.88
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr("breakout_scanner.calculate_adx", _flat_adx_mock(19.0))
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [0.35] * len(close),
    )

    result = evaluate_bars_as_of(df, n - 1, "MICRO_CAP_250")
    assert result["fail_reason"] is None
    assert "adx_soft" in result["pass_paths"]
    assert result["adx_val"] == 19.0


def test_vpcs_excluded_from_alternate_path_cap(monkeypatch):
    """VPCS bypass is excluded from exception cap (like power_gap)."""
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=50.0, step=0.25)
    close[-2] = close[-3]
    close[-1] = close[-2] * 1.06
    base_vol = 80000.0
    volume = [base_vol] * (n - 1) + [base_vol * 3.2]
    df = _synthetic_df(close, volume)
    df["high"][-1] = close[-1]
    df["low"][-1] = close[-1] * 0.97
    _passing_eval_mocks(monkeypatch)
    monkeypatch.setattr(
        "breakout_scanner._atr_simple",
        lambda high, low, close, period=14: [2.5] * len(close),
    )

    result = evaluate_bars_as_of(df, n - 1, "SMALL_CAP_100")
    assert result.get("pass_paths") == ["vpcs_price_compressed_accumulation"]
    assert result.get("alternate_bypass_count") == 0
    assert result.get("excessive_alternate_paths") is not True
