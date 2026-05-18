"""Filter ICICI scrip-master rows to listed equities (drop indices, funds, debentures, junk tickers)."""

from __future__ import annotations

import os
import re

from src.models import UniverseInstrument

_ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")

# Uppercased instrument_name substrings that indicate non-equity or non-core universe.
_NOISE_NAME_MARKERS = (
    " INDEX",
    "INDEX ",
    " ETF",
    "ETF ",
    " MUTUAL FUND",
    "MUTUAL FUND",
    " DEBENTURE",
    " NCD ",
    " REIT",
    " INVIT",
    " GOI ",
    " BEARER",
    " WARRANT",
)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def is_meaningful_listed_equity(inst: UniverseInstrument) -> bool:
    """
    Keep rows that look like NSE/BSE listed equities for sector scanning.

    Default rules:
    - ISIN must match Indian security ISIN pattern (12 chars, IN + 10 alnum).
    - instrument_name must not look like index / ETF / MF / debt product.

    Override with env:
    - EQUITY_FILTER_STRICT_ISIN=0 — allow empty/missing ISIN for NSE symbols
      matching ^[A-Z]{1,15}$ (letters only, typical equity tickers).
    """
    sym = (inst.symbol or "").strip().upper()
    if not sym:
        return False

    name_u = (inst.instrument_name or "").strip().upper()
    for m in _NOISE_NAME_MARKERS:
        if m in name_u:
            return False

    isin = (inst.isin or "").strip().upper()
    if _ISIN_RE.match(isin):
        return True

    if _env_flag("EQUITY_FILTER_STRICT_ISIN", default=True):
        return False

    # Relaxed path: NSE letter tickers only (common when ISIN missing in source row).
    ex = (inst.exchange or "").strip().upper()
    if ex == "NSE" and re.fullmatch(r"[A-Z]{1,15}", sym):
        return True

    return False
