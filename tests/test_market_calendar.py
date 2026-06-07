from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

from market_calendar import is_cash_market_session_open_ist, market_closed_reason_ist, parse_ist_holidays_env


def test_parse_ist_holidays_env_accepts_multiple_delimiters() -> None:
    parsed = parse_ist_holidays_env("2026-01-26,2026-08-15\n2026-10-02;2026-11-14")
    assert parsed == {
        date(2026, 1, 26),
        date(2026, 8, 15),
        date(2026, 10, 2),
        date(2026, 11, 14),
    }


@patch("market_calendar.load_nse_holidays_ist", return_value=set())
def test_market_closed_reason_weekend(_mock_holidays: object) -> None:
    saturday_ist = datetime(2026, 5, 16, 6, 30)
    reason = market_closed_reason_ist(saturday_ist)
    assert reason == "Indian market is closed (weekend)."


@patch("market_calendar.load_nse_holidays_ist", return_value={date(2026, 1, 26)})
def test_market_closed_reason_nse_holiday(_mock_holidays: object) -> None:
    monday_ist = datetime(2026, 1, 26, 6, 30)
    reason = market_closed_reason_ist(monday_ist)
    assert reason == "Indian market is closed (NSE trading holiday)."


@patch("market_calendar.load_nse_holidays_ist", return_value=set())
def test_market_closed_reason_open_day(_mock_holidays: object) -> None:
    open_day_ist = datetime(2026, 3, 31, 6, 30)
    reason = market_closed_reason_ist(open_day_ist)
    assert reason is None


@patch("market_calendar.load_nse_holidays_ist", return_value=set())
def test_is_cash_market_session_open_during_hours(_mock_holidays: object) -> None:
    tuesday_ist = datetime(2026, 6, 2, 10, 30)
    assert is_cash_market_session_open_ist(tuesday_ist) is True


@patch("market_calendar.load_nse_holidays_ist", return_value=set())
def test_is_cash_market_session_open_at_boundaries(_mock_holidays: object) -> None:
    tuesday = datetime(2026, 6, 2, 9, 15)
    assert is_cash_market_session_open_ist(tuesday) is True
    close = datetime(2026, 6, 2, 15, 30)
    assert is_cash_market_session_open_ist(close) is True
    before_open = datetime(2026, 6, 2, 9, 14)
    assert is_cash_market_session_open_ist(before_open) is False
    after_close = datetime(2026, 6, 2, 15, 31)
    assert is_cash_market_session_open_ist(after_close) is False


@patch("market_calendar.load_nse_holidays_ist", return_value=set())
def test_is_cash_market_session_closed_weekend(_mock_holidays: object) -> None:
    saturday_ist = datetime(2026, 6, 6, 11, 0)
    assert is_cash_market_session_open_ist(saturday_ist) is False


@patch("market_calendar.load_nse_holidays_ist", return_value={date(2026, 1, 26)})
def test_is_cash_market_session_closed_holiday(_mock_holidays: object) -> None:
    holiday_ist = datetime(2026, 1, 26, 11, 0)
    assert is_cash_market_session_open_ist(holiday_ist) is False
