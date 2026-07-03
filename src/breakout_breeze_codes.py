"""Bulk NSE → ICICI Breeze stock_code resolution for breakout scanner."""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from breeze_scrip_master import resolve_breeze_stock_code
except ImportError:
    from .breeze_scrip_master import resolve_breeze_stock_code

logger = logging.getLogger(__name__)


def _cfg_supabase_credentials(cfg: Any | None) -> tuple[str, str]:
    if cfg is not None:
        url = str(getattr(cfg, "supabase_url", "") or "").strip()
        key = str(getattr(cfg, "supabase_key", "") or "").strip()
        if url and key:
            return url, key
    return (
        os.environ.get("SUPABASE_URL", "").strip(),
        os.environ.get("SUPABASE_KEY", "").strip(),
    )


def _supabase_overlay(
    symbols: list[str],
    base_map: dict[str, str],
    *,
    supabase_url: str,
    supabase_key: str,
) -> dict[str, str]:
    """Overlay non-null market_instruments.breeze_stock_code when present."""
    if not symbols or not supabase_url.strip() or not supabase_key.strip():
        return base_map
    try:
        from supabase import create_client
    except ImportError:
        return base_map

    out = dict(base_map)
    client = create_client(supabase_url, supabase_key)
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        try:
            res = (
                client.table("market_instruments")
                .select("symbol,breeze_stock_code")
                .eq("exchange", "NSE")
                .in_("symbol", chunk)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Breeze code Supabase overlay skipped for chunk: %s", exc)
            continue
        for row in list(getattr(res, "data", None) or []):
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            code = str(row.get("breeze_stock_code") or "").strip().upper()
            if sym and code:
                out[sym] = code
    return out


def build_breeze_code_map(
    symbols: list[str],
    cfg: Any | None = None,
) -> dict[str, str]:
    """
    Resolve Breeze stock_code for each NSE symbol.

    Primary: scrip master via ``resolve_breeze_stock_code``.
    Overlay: ``market_instruments.breeze_stock_code`` when non-null (if Supabase configured).
    """
    normalized = sorted({str(s).replace(".NS", "").strip().upper() for s in symbols if str(s).strip()})
    if not normalized:
        return {}

    # Warm scrip master cache once.
    resolve_breeze_stock_code(normalized[0], "NSE")

    base_map = {sym: resolve_breeze_stock_code(sym, "NSE") for sym in normalized}

    if cfg is None:
        return base_map

    url = str(getattr(cfg, "supabase_url", "") or "").strip()
    key = str(getattr(cfg, "supabase_key", "") or "").strip()
    if not url or not key:
        return base_map

    return _supabase_overlay(normalized, base_map, supabase_url=url, supabase_key=key)


def resolve_breeze_stock_code_for_fetch(
    symbol: str,
    exchange_code: str,
    cfg: Any | None = None,
) -> str:
    """
    Resolve Breeze ``stock_code`` for live/historical fetch.

    Primary: scrip master via ``resolve_breeze_stock_code``.
    Overlay: ``market_instruments.breeze_stock_code`` when non-null (Supabase).
    """
    sym = str(symbol).strip().upper()
    ex = str(exchange_code).strip().upper()
    base = resolve_breeze_stock_code(sym, ex)
    if ex != "NSE":
        return base
    url, key = _cfg_supabase_credentials(cfg)
    if not url or not key or not sym:
        return base
    overlay = _supabase_overlay([sym], {sym: base}, supabase_url=url, supabase_key=key)
    return overlay.get(sym, base)
