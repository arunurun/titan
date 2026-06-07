"""Options chain context: F&O allowlist, sector index mapping, audit field helpers."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from titan_engine import find_call_put_oi_walls, get_pcr

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FNO_YAML = _REPO_ROOT / "config" / "fno_symbols.yaml"

# Minimal fallback when config/fno_symbols.yaml is missing (production uses YAML).
_DEFAULT_FNO_SYMBOLS: frozenset[str] = frozenset(
    {
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL",
        "HAL", "BEL",
    }
)

_FNO_CACHE: frozenset[str] | None = None


def _parse_fno_yaml_symbols(text: str) -> set[str]:
    """Minimal YAML list parser (symbols: / - NAME) without PyYAML."""
    symbols: set[str] = set()
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("symbols:"):
            in_list = True
            continue
        if in_list and stripped.startswith("- "):
            sym = stripped[2:].strip().strip('"').strip("'").upper()
            if sym:
                symbols.add(sym)
        elif in_list and not stripped.startswith("- "):
            in_list = False
    return symbols


def load_fno_symbols() -> frozenset[str]:
    """Load F&O allowlist from config/fno_symbols.yaml (or built-in default)."""
    global _FNO_CACHE
    if _FNO_CACHE is not None:
        return _FNO_CACHE
    symbols: set[str] = set()
    if _FNO_YAML.is_file():
        try:
            symbols = _parse_fno_yaml_symbols(_FNO_YAML.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read %s: %s; using default F&O list", _FNO_YAML, exc)
    if not symbols:
        symbols = set(_DEFAULT_FNO_SYMBOLS)
    _FNO_CACHE = frozenset(symbols)
    return _FNO_CACHE


def is_fno_symbol(symbol: str) -> bool:
    sym = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum() or ch == "-")
    return sym in load_fno_symbols()


def sector_options_underlying(sector_id: str) -> str:
    """Map a sector run to its benchmark index options underlying (NIFTY default)."""
    _ = sector_id
    return "NIFTY"


def spot_vs_strike_pct(spot: float, strike: float) -> float:
    if math.isnan(spot) or math.isnan(strike) or strike == 0.0:
        return float("nan")
    return ((spot / strike) - 1.0) * 100.0


def build_options_audit_fields(
    opt: dict[str, Any],
    *,
    spot: float,
) -> dict[str, Any]:
    """
    Populate audit/digest fields from a Breeze option-metrics payload.
    Returns NaN-safe defaults when chain is unavailable.
    """
    import pandas as pd

    unavailable = bool(opt.get("option_chain_unavailable"))
    if unavailable:
        return {
            "pcr": float("nan"),
            "put_oi": 0.0,
            "call_oi": 0.0,
            "put_oi_wall_strike": float("nan"),
            "call_oi_wall_strike": float("nan"),
            "spot_vs_put_wall_pct": float("nan"),
            "spot_vs_call_wall_pct": float("nan"),
            "options_expiry": None,
            "option_expiry": None,
            "option_chain_unavailable": True,
            "option_chain_unavailable_reason": opt.get("option_chain_unavailable_reason"),
            "oi_wall": {"strike": float("nan"), "oi": float("nan")},
        }

    call_df = opt.get("call_chain_df")
    put_df = opt.get("put_chain_df")
    if not isinstance(call_df, pd.DataFrame):
        call_df = pd.DataFrame(columns=["strike", "oi"])
    if not isinstance(put_df, pd.DataFrame):
        put_df = pd.DataFrame(columns=["strike", "oi"])

    walls = find_call_put_oi_walls(call_df, put_df)
    put_strike = float(walls["put_wall_strike"])
    call_strike = float(walls["call_wall_strike"])
    pcr = get_pcr(float(opt.get("put_oi", 0.0)), float(opt.get("call_oi", 0.0)))
    expiry = opt.get("expiry_date")

    return {
        "pcr": pcr,
        "put_oi": float(opt.get("put_oi", 0.0)),
        "call_oi": float(opt.get("call_oi", 0.0)),
        "put_oi_wall_strike": put_strike,
        "call_oi_wall_strike": call_strike,
        "spot_vs_put_wall_pct": spot_vs_strike_pct(spot, put_strike),
        "spot_vs_call_wall_pct": spot_vs_strike_pct(spot, call_strike),
        "options_expiry": expiry,
        "option_expiry": expiry,
        "option_chain_unavailable": False,
        "oi_wall": {
            "strike": float(walls["combined_wall_strike"]),
            "oi": float(walls["combined_wall_oi"]),
        },
        "option_chain_fallback_used": bool(opt.get("fallback_used", False)),
        "option_chain_expiry_try_index": opt.get("expiry_try_index"),
        "option_chain_expiry_tries": opt.get("expiry_tries"),
    }


def build_sector_options_digest(
    opt: dict[str, Any],
    *,
    spot: float,
    sector_id: str,
) -> dict[str, Any]:
    """Sector-level options context (one fetch per sector run)."""
    fields = build_options_audit_fields(opt, spot=spot)
    underlying = str(opt.get("underlying") or sector_options_underlying(sector_id)).upper()
    return {
        "sector": sector_id,
        "sector_options_underlying": underlying,
        "sector_pcr": fields["pcr"],
        "sector_put_wall_strike": fields["put_oi_wall_strike"],
        "sector_call_wall_strike": fields["call_oi_wall_strike"],
        "sector_options_expiry": fields["options_expiry"],
        "sector_index_spot": spot,
        "sector_spot_vs_put_wall_pct": fields["spot_vs_put_wall_pct"],
        "sector_spot_vs_call_wall_pct": fields["spot_vs_call_wall_pct"],
        "sector_option_chain_unavailable": fields["option_chain_unavailable"],
    }


def options_confirmation_note(audit: dict[str, Any]) -> str | None:
    """Short confirmation/conflict line vs tape/action for email model read."""
    if bool(audit.get("option_chain_unavailable", True)):
        return None
    spot = _sf(audit.get("close_last"))
    call_wall = _sf(audit.get("call_oi_wall_strike"))
    put_wall = _sf(audit.get("put_oi_wall_strike"))
    sell = str(audit.get("sell_signal") or "").lower()
    cmf = _sf(audit.get("cmf_20"))
    ret1d = _sf(audit.get("return_1d_pct"))

    notes: list[str] = []
    if not math.isnan(call_wall) and not math.isnan(spot):
        dist = abs(spot_vs_strike_pct(spot, call_wall))
        if dist <= 1.0 and sell in ("trim", "exit-risk"):
            notes.append("options: into call OI wall (resistance corroborates trim/exit)")
        elif dist <= 1.0:
            notes.append("options: near call OI wall (resistance overhead)")
    if not math.isnan(put_wall) and not math.isnan(spot) and spot < put_wall:
        if cmf < -0.05 or ret1d < 0:
            notes.append("options: below put OI support with weak tape")
        else:
            notes.append("options: below put OI support")
    if not notes:
        pcr = _sf(audit.get("pcr"))
        if not math.isnan(pcr):
            if pcr >= 1.2:
                notes.append("options: elevated put/call OI (defensive positioning)")
            elif pcr <= 0.8:
                notes.append("options: low put/call OI (call-heavy positioning)")
    return notes[0] if notes else None


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")
