from __future__ import annotations

from unittest.mock import MagicMock

from src.models import UniverseInstrument
from src.supabase_sync import _upsert_market_instruments


def test_upsert_market_instruments_includes_breeze_stock_code():
    client = MagicMock()
    table = MagicMock()
    upsert = MagicMock()
    execute = MagicMock()
    client.table.return_value = table
    table.upsert.return_value = upsert
    upsert.execute = execute

    instruments = [
        UniverseInstrument(
            exchange="NSE",
            symbol="NAVINFLUOR",
            instrument_name="Navin Fluorine International Ltd",
            isin="INE048G01026",
            official_sector_key="chemicals",
            official_industry="Chemicals",
            breeze_stock_code="NAVFLU",
        )
    ]

    _upsert_market_instruments(client, instruments)

    client.table.assert_called_with("market_instruments")
    rows = table.upsert.call_args[0][0]
    assert rows[0]["symbol"] == "NAVINFLUOR"
    assert rows[0]["breeze_stock_code"] == "NAVFLU"
    table.upsert.assert_called_once()
    upsert.execute.assert_called_once()
