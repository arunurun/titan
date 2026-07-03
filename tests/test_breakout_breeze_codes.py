from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_breeze_codes import build_breeze_code_map, resolve_breeze_stock_code_for_fetch  # noqa: E402


class _Cfg:
    supabase_url = "https://x.supabase.co"
    supabase_key = "service"


def test_build_breeze_code_map_scrip_master_only():
    with patch(
        "breakout_breeze_codes.resolve_breeze_stock_code",
        side_effect=lambda sym, ex: {"BEL": "BHAELE", "HAL": "HAL"}.get(sym, sym),
    ):
        result = build_breeze_code_map(["BEL", "HAL"])
    assert result["BEL"] == "BHAELE"
    assert result["HAL"] == "HAL"


def test_build_breeze_code_map_supabase_overlay():
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[{"symbol": "BEL", "breeze_stock_code": "CUSTOMBEL"}]
    )

    with patch(
        "breakout_breeze_codes.resolve_breeze_stock_code",
        side_effect=lambda sym, ex: {"BEL": "BHAELE"}.get(sym, sym),
    ), patch("supabase.create_client", return_value=mock_client):
        result = build_breeze_code_map(["BEL"], _Cfg())

    assert result["BEL"] == "CUSTOMBEL"


def test_build_breeze_code_map_empty_symbols():
    assert build_breeze_code_map([]) == {}


def test_resolve_breeze_stock_code_for_fetch_prefers_supabase_overlay():
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[{"symbol": "NAVINFLUOR", "breeze_stock_code": "NAVFLU"}]
    )

    with patch(
        "breakout_breeze_codes.resolve_breeze_stock_code",
        return_value="NAVINFLUOR",
    ), patch("supabase.create_client", return_value=mock_client):
        code = resolve_breeze_stock_code_for_fetch("NAVINFLUOR", "NSE", _Cfg())

    assert code == "NAVFLU"
