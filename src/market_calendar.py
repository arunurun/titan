from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_CASH_SESSION_OPEN_IST = time(9, 15)
_CASH_SESSION_CLOSE_IST = time(15, 30)


def parse_ist_holidays_env(raw: str) -> set[date]:
    values = [item.strip() for item in re.split(r"[,\n;]+", raw or "") if item.strip()]
    parsed: set[date] = set()
    for value in values:
        parsed.add(datetime.strptime(value, "%Y-%m-%d").date())
    return parsed


def _extract_dates_from_obj(obj: object) -> set[date]:
    found: set[date] = set()
    if isinstance(obj, dict):
        for value in obj.values():
            found.update(_extract_dates_from_obj(value))
        return found
    if isinstance(obj, list):
        for item in obj:
            found.update(_extract_dates_from_obj(item))
        return found
    if isinstance(obj, str):
        text = obj.strip()
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                found.add(datetime.strptime(text, fmt).date())
                break
            except ValueError:
                continue
    return found


def load_nse_holidays_ist() -> set[date]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        bootstrap = Request("https://www.nseindia.com", headers=headers)
        with urlopen(bootstrap, timeout=10):
            pass
        req = Request("https://www.nseindia.com/api/holiday-master?type=trading", headers=headers)
        with urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return _extract_dates_from_obj(payload)
    except Exception:  # noqa: BLE001
        return set()


def market_closed_reason_ist(now_ist: datetime | None = None) -> str | None:
    now = now_ist or datetime.now(IST)
    today_ist = now.date()
    if today_ist.weekday() >= 5:
        return "Indian market is closed (weekend)."

    env_holidays_raw = (os.environ.get("MARKET_HOLIDAYS_IST") or "").strip()
    env_holidays: set[date] = set()
    if env_holidays_raw:
        try:
            env_holidays = parse_ist_holidays_env(env_holidays_raw)
        except ValueError as exc:
            print(f"Ignoring MARKET_HOLIDAYS_IST due to parse error: {exc}")

    all_holidays = load_nse_holidays_ist() | env_holidays
    if today_ist in all_holidays:
        return "Indian market is closed (NSE trading holiday)."
    return None


def is_cash_market_session_open_ist(now_ist: datetime | None = None) -> bool:
    """True on NSE cash weekdays during regular session hours (09:15–15:30 IST)."""
    now = now_ist or datetime.now(IST)
    if market_closed_reason_ist(now) is not None:
        return False
    t = now.time()
    return _CASH_SESSION_OPEN_IST <= t <= _CASH_SESSION_CLOSE_IST
