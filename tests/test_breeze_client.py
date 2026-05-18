from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from breeze_client import (
    create_breeze_session,
    fetch_equity_data,
    fetch_nifty_data,
    fetch_nifty_option_metrics,
    fetch_nifty_option_metrics_with_expiry_fallback,
    volume_absorption_ratio,
    volume_participation_ratio,
)
from config_loader import TitanConfig


@patch("breeze_client.BreezeConnect")
def test_create_breeze_session_expired_raises_actionable(mock_cls):
    api = MagicMock()
    api.generate_session.side_effect = Exception("Session key is expired.")
    mock_cls.return_value = api
    cfg = make_cfg()
    with pytest.raises(RuntimeError, match=r"\[Breeze\] Session token expired"):
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
    # Avoid live ICICI scrip download in unit test; RELIANCE maps to RELIND on real master.
    monkeypatch.setattr(
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
        "breeze_client.resolve_breeze_stock_code",
        lambda sym, ex: sym.strip().upper(),
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
                "expiry_date": "2026-04-14T06:00:00.000Z",
            },
            {
                "call_oi": 10.0,
                "put_oi": 20.0,
                "chain_df": pd.DataFrame([{"strike": 25100.0, "oi": 30.0}]),
                "expiry_date": "2026-04-21T06:00:00.000Z",
            },
        ],
    )
    monkeypatch.setattr("breeze_client.fetch_nifty_option_metrics", mocked_fetch)
    m = fetch_nifty_option_metrics_with_expiry_fallback(breeze, max_expiry_tries=2)
    assert m["expiry_date"] == "2026-04-21T06:00:00.000Z"
    assert m["fallback_used"] is True
    assert m["expiry_try_index"] == 2
