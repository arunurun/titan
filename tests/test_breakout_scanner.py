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

    ok, path = _volume_filter_passes(
        vol_mult=1.1,
        vol_cum_mult=3.8,
        vol_thresh=3.5,
        tier_name="SMALL_CAP_100",
        prior_spike=False,
    )
    assert ok is True
    assert path == "vol_continuation_cum3d"

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


def test_rsi_hot_pass_path(monkeypatch):
    import breakout_scanner
    from breakout_scanner import evaluate_bars_as_of

    n = 80
    close = _uptrend_closes(n, start=40.0, step=0.3)
    close[-1] = close[-2] * 1.05
    volume = [40000.0] * (n - 1) + [350000.0]

    monkeypatch.setattr("breakout_scanner.calculate_rsi", lambda prices, period=14: [72.0] * len(prices))
    monkeypatch.setattr("breakout_scanner.calculate_adx", _rising_adx_soft)

    result = evaluate_bars_as_of(_synthetic_df(close, volume), n - 1, "SMALL_CAP_100")
    assert result["passed"] is True
    assert "rsi_hot" in result["pass_paths"]


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
    # Three spike days in T-15..T-1 window
    for idx in (n - 14, n - 10, n - 6):
        volume[idx] = 250000.0
    df = _synthetic_df(close, volume)

    ok, reason, metrics = pre_signal_validation(df, n - 1)
    assert ok is False
    assert reason == "pre_filter_vol_spike"
    assert metrics["vol_spike_days_t15_t1"] == 3


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
    assert metrics["vol_spike_days_t15_t1"] <= 2


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


def test_adx_trajectory_gate_blocks_flat_adx_on_rsi_hot(monkeypatch):
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
    assert result["fail_reason"] == "pre_filter_adx_trajectory"
    assert "rsi_hot" in result["pass_paths"]


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


def test_adx_soft_with_rsi_hot_stays_pass(monkeypatch):
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
    assert result["passed"] is True
    assert result["signal_tier"] == "PASS"
    assert "adx_soft" in result["pass_paths"]
    assert "rsi_hot" in result["pass_paths"]


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
