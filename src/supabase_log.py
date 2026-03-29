"""Persist audit payloads to Supabase with IST timestamps."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from supabase import create_client

from config_loader import TitanConfig

IST = ZoneInfo("Asia/Kolkata")


def save_audit_log(payload: dict[str, Any], config: TitanConfig, table: str = "audit_logs") -> dict[str, Any]:
    """
    Insert a row with `payload` plus `recorded_at_ist` ISO8601 in Asia/Kolkata.
    """
    row = {
        **payload,
        "recorded_at_ist": datetime.now(IST).isoformat(timespec="seconds"),
    }
    client = create_client(config.supabase_url, config.supabase_key)
    res = client.table(table).insert(row).execute()
    if hasattr(res, "data"):
        return {"data": res.data}
    return {"data": None}
