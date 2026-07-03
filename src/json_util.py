"""Normalize values for JSON APIs (NaN/Inf are not valid in standard JSON)."""

from __future__ import annotations

import math
import sys
from datetime import date, datetime, time
from typing import Any


def sanitize_for_json(obj: Any) -> Any:
    """Recursively normalize values for json.dumps / Supabase HTTP clients."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def ensure_utf8_stdio() -> None:
    """Windows consoles often default to cp1252; digest output uses Unicode markers."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
