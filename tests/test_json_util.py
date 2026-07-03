import json
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from json_util import sanitize_for_json


def test_sanitize_nan_inf_become_null_roundtrip():
    raw = {"a": 1.0, "b": float("nan"), "c": float("inf"), "n": {"z": float("-inf")}}
    clean = sanitize_for_json(raw)
    s = json.dumps(clean)
    assert "NaN" not in s
    assert json.loads(s)["b"] is None


def test_sanitize_datetime_and_date_are_json_serializable():
    ist = ZoneInfo("Asia/Kolkata")
    recorded = datetime(2026, 7, 3, 14, 8, 0, tzinfo=ist)
    raw = {
        "prompt_facts": {
            "comparison": {
                "today": {"run_ts": recorded, "trade_date": date(2026, 7, 3)},
            }
        }
    }
    clean = sanitize_for_json(raw)
    s = json.dumps(clean)
    parsed = json.loads(s)
    assert parsed["prompt_facts"]["comparison"]["today"]["run_ts"].startswith("2026-07-03T14:08:00")
    assert parsed["prompt_facts"]["comparison"]["today"]["trade_date"] == "2026-07-03"
