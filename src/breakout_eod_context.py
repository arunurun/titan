"""NSE EOD delivery context for institutional flow enrichment."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _parse_as_of(as_of_date: str | None) -> date | None:
    if not as_of_date:
        return None
    raw = str(as_of_date).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def load_delivery_pct_by_symbol(
    symbols: list[str],
    *,
    as_of_date: str | None = None,
    lookback_sessions: int = 5,
) -> dict[str, float]:
    """
    Load NSE delivery % (DELIV_PER) per symbol from sec_bhavdata_full.

    Walks back up to ``lookback_sessions`` weekdays when the as-of session is missing
    (holiday / delayed bhavcopy). Returns upper-case symbol → delivery %.
    """
    want = {str(s).strip().upper() for s in symbols if str(s).strip()}
    if not want:
        return {}

    try:
        from nse_eod import fetch_sec_bhavdata_full
    except ImportError:
        return {}

    anchor = _parse_as_of(as_of_date) or date.today()
    out: dict[str, float] = {}
    remaining = set(want)
    checked = 0
    d = anchor
    while remaining and checked < max(1, int(lookback_sessions)):
        if d.weekday() < 5:
            checked += 1
            try:
                frame = fetch_sec_bhavdata_full(d)
            except Exception as exc:  # noqa: BLE001
                logger.debug("delivery bhavcopy %s failed: %s", d.isoformat(), exc)
                frame = pd.DataFrame()
            if not frame.empty and "symbol" in frame.columns:
                sub = frame[frame["symbol"].astype(str).str.upper().isin(remaining)]
                for rec in sub.to_dict("records"):
                    sym = str(rec.get("symbol") or "").strip().upper()
                    deliv = rec.get("deliv_per")
                    if sym and deliv is not None and pd.notna(deliv):
                        out[sym] = round(float(deliv), 2)
                        remaining.discard(sym)
        d -= timedelta(days=1)
    return out
