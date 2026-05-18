"""Sector registry tests (Supabase primary, CSV fallback)."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from sector_registry import SectorInstrument, list_active_sector_ids, load_sector_instruments, load_sector_symbols


class _FakeQuery:
    def __init__(self, payload):
        self.payload = payload

    def select(self, _fields):
        return self

    def eq(self, _key, _val):
        return self

    def in_(self, _key, _vals):
        return self

    def execute(self):
        return SimpleNamespace(data=self.payload)


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def table(self, _name):
        return _FakeQuery(self.payload)


def _mock_supabase(monkeypatch, payload):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "service-role-test-key")
    monkeypatch.setattr("sector_registry.create_client", lambda _u, _k: _FakeClient(payload))


def test_load_sector_respects_max_symbols(monkeypatch):
    _mock_supabase(
        monkeypatch,
        [
            {"market_instruments": {"symbol": "A", "exchange": "NSE"}},
            {"market_instruments": {"symbol": "B", "exchange": "NSE"}},
            {"market_instruments": {"symbol": "C", "exchange": "NSE"}},
        ],
    )
    assert load_sector_symbols("defence", max_symbols=2) == ["A", "B"]
    inst = load_sector_instruments("defence", max_symbols=2)
    assert inst == [SectorInstrument("A", "NSE"), SectorInstrument("B", "NSE")]


def test_load_sector_returns_all_when_max_symbols_none(monkeypatch):
    payload = [
        {"market_instruments": {"symbol": f"S{x}", "exchange": "NSE"}}
        for x in range(85)
    ]
    _mock_supabase(monkeypatch, payload)
    out = load_sector_symbols("defence", max_symbols=None)
    assert len(out) == 85


def test_dedupe_preserves_order(monkeypatch):
    _mock_supabase(
        monkeypatch,
        [
            {"market_instruments": {"symbol": "HAL", "exchange": "NSE"}},
            {"market_instruments": {"symbol": "hal", "exchange": "NSE"}},
            {"market_instruments": {"symbol": "BEL", "exchange": "NSE"}},
        ],
    )
    assert load_sector_symbols("defence", max_symbols=10) == ["HAL", "BEL"]


def test_missing_supabase_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Supabase load failed and CSV fallback missing/empty"):
        load_sector_symbols("unknown_sector")


def test_empty_sector_mapping_raises(monkeypatch):
    _mock_supabase(monkeypatch, [])
    with pytest.raises(RuntimeError, match="No active instruments mapped"):
        load_sector_symbols("unknown_sector")


def test_prefers_nse_when_symbol_exists_in_both_exchanges(monkeypatch):
    _mock_supabase(
        monkeypatch,
        [
            {"market_instruments": {"symbol": "FOO", "exchange": "NSE"}},
            {"market_instruments": {"symbol": "FOO", "exchange": "BSE"}},
        ],
    )
    inst = load_sector_instruments("defence")
    assert inst == [SectorInstrument("FOO", "NSE")]


def test_keeps_bse_when_nse_missing(monkeypatch):
    _mock_supabase(
        monkeypatch,
        [
            {"market_instruments": {"symbol": "ONLYBSE", "exchange": "BSE"}},
        ],
    )
    inst = load_sector_instruments("defence")
    assert inst == [SectorInstrument("ONLYBSE", "BSE")]


def test_invalid_exchange_raises(monkeypatch, tmp_path: Path):
    sectors_dir = tmp_path / "sectors"
    sectors_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("sector_registry.SECTORS_DIR", sectors_dir)
    _mock_supabase(monkeypatch, [{"market_instruments": {"symbol": "X", "exchange": "NYSE"}}])
    with pytest.raises(RuntimeError, match="Invalid exchange"):
        load_sector_symbols("no_csv_fallback")


def test_falls_back_to_csv_when_supabase_missing(monkeypatch, tmp_path: Path):
    sectors_dir = tmp_path / "sectors"
    sectors_dir.mkdir(parents=True, exist_ok=True)
    (sectors_dir / "defence.csv").write_text(
        "symbol,exchange\nHAL,NSE\nBEL,NSE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sector_registry.SECTORS_DIR", sectors_dir)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    out = load_sector_symbols("defence")
    assert out == ["HAL", "BEL"]


def test_falls_back_to_csv_when_supabase_returns_empty(monkeypatch, tmp_path: Path):
    sectors_dir = tmp_path / "sectors"
    sectors_dir.mkdir(parents=True, exist_ok=True)
    (sectors_dir / "defence.csv").write_text(
        "symbol,exchange\nHAL,NSE\nBDL,NSE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sector_registry.SECTORS_DIR", sectors_dir)
    _mock_supabase(monkeypatch, [])
    out = load_sector_symbols("defence")
    assert out == ["HAL", "BDL"]


def test_list_active_sector_ids_from_supabase(monkeypatch):
    _mock_supabase(monkeypatch, [{"sector_key": "defence"}, {"sector_key": "unknown"}])
    out = list_active_sector_ids(include_unknown=False)
    assert out == ["defence"]


def test_list_active_sector_ids_from_csv_fallback(monkeypatch, tmp_path: Path):
    sectors_dir = tmp_path / "sectors"
    sectors_dir.mkdir(parents=True, exist_ok=True)
    (sectors_dir / "defence.csv").write_text("symbol,exchange\nHAL,NSE\n", encoding="utf-8")
    (sectors_dir / "auto.csv").write_text("symbol,exchange\nTATAMOTORS,NSE\n", encoding="utf-8")
    monkeypatch.setattr("sector_registry.SECTORS_DIR", sectors_dir)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    out = list_active_sector_ids(include_unknown=True)
    assert out == ["auto", "defence"]
