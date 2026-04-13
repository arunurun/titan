"""Persist audit payloads to Supabase with IST timestamps."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig
from json_util import sanitize_for_json

IST = ZoneInfo("Asia/Kolkata")


def save_audit_log(payload: dict[str, Any], config: TitanConfig, table: str = "audit_logs") -> dict[str, Any]:
    """
    Insert a row with `payload` plus `recorded_at_ist` ISO8601 in Asia/Kolkata.
    """
    row = {
        **sanitize_for_json(payload),
        "recorded_at_ist": datetime.now(IST).isoformat(timespec="seconds"),
    }
    client = create_client(config.supabase_url, config.supabase_key)
    try:
        res = client.table(table).insert(row).execute()
    except APIError as e:
        payload = e.args[0] if e.args else {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        if code == "PGRST205" or "could not find the table" in msg.lower():
            raise RuntimeError(
                "[Supabase] Table missing or not exposed (REST). "
                f"Expected public.{table}. Create it in Supabase SQL Editor (see sql/create_audit_logs.sql). "
                f"PostgREST: {code or 'n/a'} — {msg}"
            ) from e
        raise RuntimeError(f"[Supabase] Persist failed ({code or 'error'}): {msg}") from e
    if hasattr(res, "data"):
        return {"data": res.data}
    return {"data": None}
