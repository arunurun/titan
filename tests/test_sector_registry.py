"""Sector CSV registry."""

from pathlib import Path

import pytest

from sector_registry import (
    MAX_SYMBOLS,
    ROOT,
    SECTORS_DIR,
    SectorInstrument,
    load_sector_instruments,
    load_sector_symbols,
)


def test_load_defence_respects_max_symbols(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text(
        "symbol,exchange\nA,NSE\nB,NSE\nC,NSE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    assert load_sector_symbols("defence", max_symbols=2) == ["A", "B"]
    inst = load_sector_instruments("defence", max_symbols=2)
    assert inst == [
        SectorInstrument("A", "NSE"),
        SectorInstrument("B", "NSE"),
    ]


def test_load_defence_uses_module_cap_when_none(monkeypatch, tmp_path):
    letters = "\n".join(f"{x},NSE" for x in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text(f"symbol,exchange\n{letters}\n", encoding="utf-8")
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    out = load_sector_symbols("defence", max_symbols=None)
    assert len(out) == min(MAX_SYMBOLS, 26)


def test_dedupe_preserves_order(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text(
        "symbol,exchange\nHAL,NSE\nhal,NSE\nBEL,NSE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    assert load_sector_symbols("defence", max_symbols=10) == ["HAL", "BEL"]


def test_missing_file():
    with pytest.raises(FileNotFoundError, match="No sector file"):
        load_sector_symbols("nonexistent_sector_xyz")


def test_missing_symbol_column(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text("ticker\nX\n", encoding="utf-8")
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    with pytest.raises(ValueError, match="symbol"):
        load_sector_symbols("defence")


def test_exchange_defaults_to_nse_when_column_missing(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text("symbol\nX\n", encoding="utf-8")
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    assert load_sector_instruments("defence") == [SectorInstrument("X", "NSE")]


def test_bse_rows_and_dedupe_by_exchange(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text(
        "symbol,exchange\nFOO,NSE\nFOO,BSE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    inst = load_sector_instruments("defence")
    assert len(inst) == 2
    assert SectorInstrument("FOO", "NSE") in inst
    assert SectorInstrument("FOO", "BSE") in inst


def test_invalid_exchange_raises(monkeypatch, tmp_path):
    csv_path = tmp_path / "defence.csv"
    csv_path.write_text("symbol,exchange\nX,NYSE\n", encoding="utf-8")
    monkeypatch.setattr("sector_registry.SECTORS_DIR", tmp_path)
    with pytest.raises(ValueError, match="Invalid exchange"):
        load_sector_instruments("defence")


def test_real_defence_file_exists():
    p = SECTORS_DIR / "defence.csv"
    assert p.is_file(), f"expected {p} under repo"
    inst = load_sector_instruments("defence", max_symbols=100)
    assert len(inst) >= 1
    assert all(i.symbol == i.symbol.upper() for i in inst)
    exchanges = {i.exchange for i in inst}
    assert "NSE" in exchanges
    assert "BSE" in exchanges
    assert any(i.symbol == "SIKA" and i.exchange == "BSE" for i in inst)
    assert any(i.symbol == "HIGHENE" and i.exchange == "BSE" for i in inst)
    assert any(i.symbol == "CFF" and i.exchange == "BSE" for i in inst)
    assert any(i.symbol == "TANEJAERO" and i.exchange == "BSE" for i in inst)


def test_sectors_dir_under_data():
    assert SECTORS_DIR == ROOT / "data" / "sectors"
    assert (ROOT / "data" / "sectors").is_dir()
