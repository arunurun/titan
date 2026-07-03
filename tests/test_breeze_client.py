from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import threading
import time

from breeze_client import (
    BreezeDataStaleError,
    _classify_breeze_option_chain_response,
    _rate_limited_historical_call,
    breeze_data_stale_reason,
    clear_breeze_data_stale_for_tests,
    create_breeze_session,
    is_breeze_data_stale,
    fetch_equity_data,
    fetch_equity_quote,
    fetch_nifty_data,
    fetch_nifty_option_metrics,
    fetch_nifty_option_metrics_with_expiry_fallback,
    fetch_option_metrics_for_underlying,
    fetch_option_metrics_with_expiry_fallback,
    nfo_underlying_code_candidates,
    volume_absorption_ratio,
    volume_participation_ratio,
)
from config_loader import TitanConfig


@patch("breeze_client.BreezeConnect")
def test_create_breeze_session_expired_raises_actionable(mock_cls):
    clear_breeze_data_stale_for_tests()
    api = MagicMock()
    api.generate_session.side_effect = Exception("Session key is expired.")
    mock_cls.return_value = api
    cfg = make_cfg()
    with pytest.raises(BreezeDataStaleError, match=r"\[Breeze\] Session token expired"):
        create_breeze_session(cfg)
    assert is_breeze_data_stale()
    assert "expired" in breeze_data_stale_reason().lower()


def test_create_breeze_session_missing_token_marks_stale():
    clear_breeze_data_stale_for_tests()
    cfg = make_cfg()
    cfg = TitanConfig(
        breeze_api_key=cfg.breeze_api_key,
        breeze_secret=cfg.breeze_secret,
        breeze_session_token="",
        gemini_api_keys=cfg.gemini_api_keys,
        supabase_url=cfg.supabase_url,
        supabase_key=cfg.supabase_key,
    )
    with pytest.raises(BreezeDataStaleError, match=r"BREEZE_SESSION_TOKEN missing"):
        create_breeze_session(cfg)
    assert is_breeze_data_stale()


def test_create_breeze_session_blocked_in_reconcile_mode(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setenv("TITAN_RECONCILE_MODE", "1")
    with pytest.raises(RuntimeError, match=r"\[ReconcileGuard\]"):
        create_breeze_session(cfg)


def make_cfg():
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


@patch("breeze_client.BreezeConnect")
def test_fetch_nifty_data_success(mock_breeze_cls):
    api = MagicMock()
    api.get_historical_data.return_value = {
        "Success": [{"close": 100.0, "datetime": "2024-01-01"}],
    }
    mock_breeze_cls.return_value = api
    df = fetch_nifty_data(make_cfg(), max_retries=1)
    assert len(df) == 1


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_passes_symbol_exchange(mock_breeze_cls, monkeypatch):
    # Avoid live ICICI scrip download; mock fetch resolver so input symbol passes through.
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_historical_data.return_value = {
        "Success": [{"close": 50.0, "volume": 1000, "datetime": "2024-01-01"}],
    }
    mock_breeze_cls.return_value = api
    df = fetch_equity_data(make_cfg(), "RELIANCE", "NSE", breeze=api, max_retries=0)
    assert len(df) == 1
    call_kw = api.get_historical_data.call_args[1]
    assert call_kw["stock_code"] == "RELIANCE"
    assert call_kw["exchange_code"] == "NSE"


@patch("breeze_client.BreezeConnect")
def test_fetch_raises_after_retries(mock_breeze_cls):
    api = MagicMock()
    api.get_historical_data.side_effect = RuntimeError("network")
    mock_breeze_cls.return_value = api
    with pytest.raises(RuntimeError, match="failed after retries"):
        fetch_nifty_data(make_cfg(), max_retries=2)


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_no_data_found_returns_empty_and_does_not_retry(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_historical_data.return_value = {
        "Success": None,
        "Status": 200,
        "Error": "No Data Found",
    }
    mock_breeze_cls.return_value = api

    df = fetch_equity_data(
        make_cfg(),
        "AICHAMP",
        "NSE",
        breeze=api,
        max_retries=4,
        allow_exchange_fallback=False,
    )

    assert df.empty
    assert api.get_historical_data.call_count == 1


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_historical_data_fail_treated_as_soft_no_data(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_historical_data.return_value = {
        "Success": None,
        "Status": 500,
        "Error": "Historical Data Fail",
    }
    mock_breeze_cls.return_value = api

    df = fetch_equity_data(
        make_cfg(),
        "BADSYM",
        "NSE",
        breeze=api,
        max_retries=4,
        allow_exchange_fallback=False,
    )

    assert df.empty
    assert api.get_historical_data.call_count == 1


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_fallback_to_bse_when_nse_has_no_data(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_historical_data.side_effect = [
        {"Success": None, "Status": 200, "Error": "No Data Found"},
        {"Success": [{"close": 10.0, "volume": 100.0, "datetime": "2024-01-01"}]},
    ]
    mock_breeze_cls.return_value = api

    df = fetch_equity_data(make_cfg(), "FOO", "NSE", breeze=api, max_retries=0)

    assert len(df) == 1
    assert df.attrs.get("exchange_requested") == "NSE"
    assert df.attrs.get("exchange_used") == "BSE"
    assert df.attrs.get("exchange_fallback_used") is True
    first_call = api.get_historical_data.call_args_list[0].kwargs
    second_call = api.get_historical_data.call_args_list[1].kwargs
    assert first_call["exchange_code"] == "NSE"
    assert second_call["exchange_code"] == "BSE"


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_primary_exchange_metadata(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_historical_data.return_value = {
        "Success": [{"close": 21.0, "volume": 100.0, "datetime": "2024-01-01"}],
    }
    mock_breeze_cls.return_value = api

    df = fetch_equity_data(make_cfg(), "BAR", "NSE", breeze=api, max_retries=0)

    assert len(df) == 1
    assert df.attrs.get("exchange_requested") == "NSE"
    assert df.attrs.get("exchange_used") == "NSE"
    assert df.attrs.get("exchange_fallback_used") is False


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_rate_limited_then_success_retries(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    monkeypatch.setattr("breeze_client.time.sleep", lambda _s: None)
    api = MagicMock()
    api.get_historical_data.side_effect = [
        {"Success": None, "Status": 5, "Error": "Limit exceed: API call per minute:Try after some time"},
        {"Success": [{"close": 99.0, "volume": 1000.0, "datetime": "2024-01-01"}]},
    ]
    mock_breeze_cls.return_value = api

    df = fetch_equity_data(make_cfg(), "RATELIM", "NSE", breeze=api, max_retries=2)

    assert len(df) == 1
    assert api.get_historical_data.call_count == 2


def test_rate_limited_call_does_not_hold_lock_during_network(monkeypatch):
    monkeypatch.setattr("breeze_client._min_hist_call_interval_seconds", lambda: 0.0)
    monkeypatch.setattr("breeze_client._historical_call_timeout_seconds", lambda: 1.0)
    monkeypatch.setattr("breeze_client._LAST_HIST_CALL_AT", 0.0)

    class _FakeBreeze:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()
            self.unblock = threading.Event()

        def get_historical_data(self, **_kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.unblock.wait(timeout=0.5)
            with self.lock:
                self.active -= 1
            return {"Success": [{"close": 1.0}]}

    breeze = _FakeBreeze()
    t1 = threading.Thread(target=lambda: _rate_limited_historical_call(breeze, interval="1day"))
    t2 = threading.Thread(target=lambda: _rate_limited_historical_call(breeze, interval="1day"))
    t1.start()
    time.sleep(0.1)
    t2.start()
    time.sleep(0.1)
    breeze.unblock.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert breeze.max_active >= 2


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_data_timeout_marks_hard_failure(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    monkeypatch.setattr("breeze_client._min_hist_call_interval_seconds", lambda: 0.0)
    monkeypatch.setattr("breeze_client._historical_call_timeout_seconds", lambda: 0.05)

    api = MagicMock()

    def _sleeping_call(**_kwargs):
        time.sleep(0.2)
        return {"Success": [{"close": 9.0}]}

    api.get_historical_data.side_effect = _sleeping_call
    mock_breeze_cls.return_value = api

    with pytest.raises(RuntimeError, match="historical fetch timeout"):
        fetch_equity_data(
            make_cfg(),
            "SLOW",
            "NSE",
            breeze=api,
            max_retries=1,
            allow_exchange_fallback=False,
        )


def test_volume_participation_ratio_matches_legacy_alias():
    df = pd.DataFrame({"volume": [100.0, 100.0, 200.0]})
    assert volume_participation_ratio(df) == volume_absorption_ratio(df)


    df = pd.DataFrame(
        {
            "volume": [100.0, 100.0, 100.0, 100.0, 100.0, 150.0],
        }
    )
    r = volume_absorption_ratio(df)
    assert abs(r - 150.0 / 100.0) < 1e-9


def test_fetch_nifty_option_metrics_aggregates():
    breeze = MagicMock()
    breeze.get_option_chain_quotes.side_effect = [
        {
            "Success": [
                {"strike_price": 22000.0, "open_interest": "100"},
                {"strike_price": 22100.0, "open_interest": 200.0},
            ],
        },
        {
            "Success": [
                {"strike_price": 22000.0, "open_interest": 50.0},
                {"strike_price": 22200.0, "open_interest": 25.0},
            ],
        },
    ]
    m = fetch_nifty_option_metrics(breeze, "2026-04-02T00:00:00.000Z")
    assert m["call_oi"] == 300.0
    assert m["put_oi"] == 75.0
    row_22k = m["chain_df"][m["chain_df"]["strike"] == 22000.0].iloc[0]
    assert row_22k["oi"] == 150.0
    assert not m["call_chain_df"].empty
    assert not m["put_chain_df"].empty


def test_fetch_option_metrics_for_underlying_stock():
    breeze = MagicMock()
    breeze.get_option_chain_quotes.side_effect = [
        {"Success": [{"strike_price": 2500.0, "open_interest": 1000.0}]},
        {"Success": [{"strike_price": 2400.0, "open_interest": 2000.0}]},
    ]
    m = fetch_option_metrics_for_underlying(breeze, "RELIANCE", "2026-06-24T06:00:00.000Z")
    assert m["underlying"] == "RELIANCE"
    assert m["call_oi"] == 1000.0
    assert m["put_oi"] == 2000.0


def test_nfo_underlying_code_candidates_tries_breeze_alias():
    assert nfo_underlying_code_candidates("BEL") == ["BHAELE", "BEL"]
    assert nfo_underlying_code_candidates("NIFTY") == ["NIFTY"]


def test_nfo_underlying_code_candidates_industower_resolves_to_bhainf():
    assert nfo_underlying_code_candidates("INDUSTOWER") == ["BHAINF", "INDUSTOWER"]


def test_fetch_option_metrics_with_fallback_uses_breeze_nfo_code(monkeypatch):
    monkeypatch.setattr(
        "breeze_client.expiry_candidates_for_underlying",
        lambda code, max_tries=8: ["2026-06-30T06:00:00.000Z"],
    )

    breeze = MagicMock()

    def _fetch(_breeze, nfo_code, _expiry):
        if nfo_code == "RELIANCE":
            return {
                "underlying": "RELIANCE",
                "call_oi": 0.0,
                "put_oi": 0.0,
                "chain_df": __import__("pandas").DataFrame(columns=["strike", "oi"]),
                "call_chain_df": __import__("pandas").DataFrame(columns=["strike", "oi"]),
                "put_chain_df": __import__("pandas").DataFrame(columns=["strike", "oi"]),
                "expiry_date": _expiry,
            }
        return {
            "underlying": "RELIANCE",
            "call_oi": 500.0,
            "put_oi": 700.0,
            "chain_df": __import__("pandas").DataFrame({"strike": [2500.0], "oi": [500.0]}),
            "call_chain_df": __import__("pandas").DataFrame({"strike": [2500.0], "oi": [500.0]}),
            "put_chain_df": __import__("pandas").DataFrame({"strike": [2400.0], "oi": [700.0]}),
            "expiry_date": _expiry,
        }

    monkeypatch.setattr("breeze_client.fetch_option_metrics_for_underlying", _fetch)

    m = fetch_option_metrics_with_expiry_fallback(breeze, "RELIANCE", max_expiry_tries=1)
    assert m.get("option_chain_unavailable") is not True
    assert m["nfo_stock_code"] == "RELIND"
    assert m["call_oi"] == 500.0


def test_fetch_option_metrics_with_fallback_degrades():
    breeze = MagicMock()
    no_data = {"Success": None, "Status": 500, "Error": "No Data Found"}
    breeze.get_option_chain_quotes.return_value = no_data
    m = fetch_option_metrics_with_expiry_fallback(breeze, "RELIANCE", max_expiry_tries=2)
    assert m.get("option_chain_unavailable") is True
    assert m.get("option_chain_unavailable_reason")


def test_fetch_nifty_option_metrics_no_data_found_returns_empty():
    breeze = MagicMock()
    no_data = {"Success": None, "Status": 500, "Error": "No Data Found"}
    breeze.get_option_chain_quotes.return_value = no_data
    m = fetch_nifty_option_metrics(breeze, "2026-04-07T06:00:00.000Z")
    assert m["call_oi"] == 0.0 and m["put_oi"] == 0.0
    assert m["chain_df"].empty


def test_fetch_nifty_option_metrics_with_fallback_degrades_gracefully():
    breeze = MagicMock()
    no_data = {"Success": None, "Status": 500, "Error": "No Data Found"}
    breeze.get_option_chain_quotes.return_value = no_data
    m = fetch_nifty_option_metrics_with_expiry_fallback(breeze, max_expiry_tries=2)
    assert m.get("option_chain_unavailable") is True
    assert m["put_oi"] == 0.0 and m["call_oi"] == 0.0


def test_classify_breeze_option_chain_response():
    assert _classify_breeze_option_chain_response({"Success": []}) == "ok"
    assert _classify_breeze_option_chain_response({"Success": None, "Error": "No Data Found"}) == "no_chain_data"
    assert (
        _classify_breeze_option_chain_response(
            {
                "Success": None,
                "Status": 500,
                "Error": "Error while calling service, Please contact admin (T10:56)",
            }
        )
        == "breeze_service_error"
    )


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_quote_normalizes_fields(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_quotes.return_value = {
        "Success": [
            {
                "exchange_code": "NSE",
                "ltp": 101.5,
                "previous_close": 100.0,
                "ltp_percent_change": 1.5,
                "ltt": "06-Jun-2026 14:32:00",
                "open": 99.0,
                "high": 102.0,
                "low": 98.5,
            },
            {"exchange_code": "NA", "ltp": 0.0},
        ],
    }
    mock_breeze_cls.return_value = api

    quote = fetch_equity_quote(make_cfg(), "RELIANCE", "NSE", breeze=api, max_retries=0)

    assert quote["ltp"] == 101.5
    assert quote["previous_close"] == 100.0
    assert quote["ltp_percent_change"] == 1.5
    assert quote["ltt"] == "06-Jun-2026 14:32:00"
    assert quote["open"] == 99.0
    call_kw = api.get_quotes.call_args[1]
    assert call_kw["product_type"] == "cash"
    assert call_kw["stock_code"] == "RELIANCE"


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_quote_no_data_returns_empty(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    api = MagicMock()
    api.get_quotes.return_value = {
        "Success": None,
        "Status": 200,
        "Error": "No Data Found",
    }
    mock_breeze_cls.return_value = api

    quote = fetch_equity_quote(make_cfg(), "MISSING", "NSE", breeze=api, max_retries=0)

    assert quote == {}


@patch("breeze_client.BreezeConnect")
def test_fetch_equity_quote_rate_limited_then_success(mock_breeze_cls, monkeypatch):
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code_for_fetch",
        lambda sym, ex, cfg=None: sym.strip().upper(),
    )
    monkeypatch.setattr("breeze_client.time.sleep", lambda _s: None)
    api = MagicMock()
    api.get_quotes.side_effect = [
        {"Success": None, "Status": 5, "Error": "Limit exceed: API call per minute:Try after some time"},
        {
            "Success": [
                {
                    "exchange_code": "NSE",
                    "ltp": 50.0,
                    "previous_close": 49.0,
                    "ltp_percent_change": 2.04,
                    "ltt": "06-Jun-2026 10:00:00",
                    "open": 49.5,
                    "high": 50.5,
                    "low": 49.0,
                }
            ],
        },
    ]
    mock_breeze_cls.return_value = api

    quote = fetch_equity_quote(make_cfg(), "FOO", "NSE", breeze=api, max_retries=2)

    assert quote["ltp"] == 50.0
    assert api.get_quotes.call_count == 2


def test_fetch_nifty_option_metrics_with_fallback_skips_zero_oi_chain(monkeypatch):
    breeze = MagicMock()
    monkeypatch.setattr(
        "breeze_client.iter_weekly_expiry_candidates",
        lambda weeks_ahead=8, reference=None: [
            "2026-04-14T06:00:00.000Z",
            "2026-04-21T06:00:00.000Z",
        ],
    )
    mocked_fetch = MagicMock(
        side_effect=[
            {
                "call_oi": 0.0,
                "put_oi": 0.0,
                "chain_df": pd.DataFrame([{"strike": 25000.0, "oi": 0.0}]),
                "call_chain_df": pd.DataFrame([{"strike": 25000.0, "oi": 0.0}]),
                "put_chain_df": pd.DataFrame([{"strike": 25000.0, "oi": 0.0}]),
                "expiry_date": "2026-04-14T06:00:00.000Z",
            },
            {
                "call_oi": 10.0,
                "put_oi": 20.0,
                "chain_df": pd.DataFrame([{"strike": 25100.0, "oi": 30.0}]),
                "call_chain_df": pd.DataFrame([{"strike": 25100.0, "oi": 10.0}]),
                "put_chain_df": pd.DataFrame([{"strike": 25100.0, "oi": 20.0}]),
                "expiry_date": "2026-04-21T06:00:00.000Z",
            },
        ],
    )
    monkeypatch.setattr("breeze_client.fetch_option_metrics_for_underlying", mocked_fetch)
    m = fetch_nifty_option_metrics_with_expiry_fallback(breeze, max_expiry_tries=2)
    assert m["expiry_date"] == "2026-04-21T06:00:00.000Z"
    assert m["fallback_used"] is True
    assert m["expiry_try_index"] == 2
