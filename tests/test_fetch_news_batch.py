"""Tests for scripts/fetch_news_batch.py priority scope."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from config_loader import TitanConfig
from sector_registry import SectorInstrument


def _load_fetch_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "fetch_news_batch.py"
    spec = importlib.util.spec_from_file_location("fetch_news_batch", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def test_collect_symbol_pairs_priority_only_no_full_fallback(monkeypatch):
    mod = _load_fetch_module()
    monkeypatch.setattr(
        mod,
        "load_priority_instruments",
        lambda _cfg, sector_key, top_n=None: [],
    )
    monkeypatch.setattr(
        mod,
        "load_sector_instruments",
        lambda _sector: [SectorInstrument("ZZZ", "NSE")],
    )
    pairs = mod._collect_symbol_pairs(
        make_cfg(),
        ["defence"],
        priority_only=True,
        priority_top_n=10,
    )
    assert pairs == []


def test_collect_symbol_pairs_priority_only_uses_priority_list(monkeypatch):
    mod = _load_fetch_module()
    monkeypatch.setattr(
        mod,
        "load_priority_instruments",
        lambda _cfg, sector_key, top_n=None: [
            SectorInstrument("HAL", "NSE"),
            SectorInstrument("BEL", "NSE"),
        ],
    )
    pairs = mod._collect_symbol_pairs(
        make_cfg(),
        ["defence"],
        priority_only=True,
        priority_top_n=None,
    )
    assert len(pairs) == 2
    symbols = {p[0] for p in pairs}
    assert symbols == {"HAL", "BEL"}


def test_fetch_and_store_for_symbol_logs_start_and_result(caplog, monkeypatch):
    mod = _load_fetch_module()
    cfg = make_cfg()

    monkeypatch.setattr(
        mod,
        "fetch_all_news_for_symbol",
        lambda _sym, _ex, cfg=None: [{"symbol": "HAL", "exchange": "NSE", "title": "t", "url": "u"}],
    )
    monkeypatch.setattr(
        mod,
        "store_news_items",
        lambda _cfg, _items: {"inserted": 1, "duplicates_skipped": 0},
    )

    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        result = mod.fetch_and_store_for_symbol(cfg, "HAL", "NSE", refresh_snapshots=False)

    assert result["error"] is None
    messages = [rec.getMessage() for rec in caplog.records if rec.name == mod.logger.name]
    assert any("[news] HAL (NSE) — fetch start" in msg for msg in messages)
    assert any("[news] HAL (NSE) — stored 1 items" in msg for msg in messages)
