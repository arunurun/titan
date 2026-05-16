from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

from market_calendar import market_closed_reason_ist, parse_ist_holidays_env


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
