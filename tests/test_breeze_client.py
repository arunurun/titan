from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from breeze_client import (
    create_breeze_session,
    fetch_nifty_data,
    fetch_nifty_option_metrics,
    fetch_nifty_option_metrics_with_expiry_fallback,
    volume_absorption_ratio,
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
        gemini_api_key="g",
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
def test_fetch_raises_after_retries(mock_breeze_cls):
    api = MagicMock()
    api.get_historical_data.side_effect = RuntimeError("network")
    mock_breeze_cls.return_value = api
    with pytest.raises(RuntimeError, match="failed after retries"):
        fetch_nifty_data(make_cfg(), max_retries=2)


def test_volume_absorption_ratio_trailing_avg():
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
