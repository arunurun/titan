"""Tests for scripts/preflight_news.py (mocked Supabase)."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config_loader import TitanConfig


def _load_preflight_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "preflight_news.py"
    spec = importlib.util.spec_from_file_location("preflight_news", script_path)
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


def test_check_global_snapshot_fresh(monkeypatch):
    mod = _load_preflight_module()
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        mod,
        "_load_latest_news_snapshot",
        lambda _cfg: {
            "refreshed_at": (now - timedelta(minutes=30)).isoformat(),
            "item_count": 12,
            "fetch_status": "ok",
        },
    )
    monkeypatch.setenv("TITAN_NEWS_SNAPSHOT_TTL_HOURS", "2")
    status = mod.check_global_snapshot(make_cfg(), now_utc=now)
    assert status["ok"] is True
    assert status["level"] == "ok"
    assert status["item_count"] == 12


def test_check_global_snapshot_stale_is_warning(monkeypatch):
    mod = _load_preflight_module()
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        mod,
        "_load_latest_news_snapshot",
        lambda _cfg: {
            "refreshed_at": (now - timedelta(hours=5)).isoformat(),
            "item_count": 8,
            "fetch_status": "ok",
        },
    )
    status = mod.check_global_snapshot(make_cfg(), now_utc=now)
    assert status["ok"] is False
    assert status["level"] == "warning"


def test_check_global_snapshot_missing(monkeypatch):
    mod = _load_preflight_module()
    monkeypatch.setattr(mod, "_load_latest_news_snapshot", lambda _cfg: None)
    status = mod.check_global_snapshot(make_cfg())
    assert status["ok"] is False
    assert status["level"] == "error"


def test_check_sector_symbol_news_counts_missing(monkeypatch):
    mod = _load_preflight_module()
    from sector_registry import SectorInstrument

    monkeypatch.setattr(
        mod,
        "_resolve_sector_symbols",
        lambda _cfg, sector_id, priority_only=False, priority_top_n=None: [
            ("HAL", "NSE"),
            ("BEL", "NSE"),
        ],
    )
    monkeypatch.setattr(mod, "get_recent_news_for_symbol", lambda *a, **k: [])
    status = mod.check_sector_symbol_news(make_cfg(), "defence")
    assert status is not None
    assert status["ok"] is False
    assert status["symbols_checked"] == 2
    assert status["symbols_stale_or_missing"] == 2


def test_resolve_sector_symbols_priority_only_no_full_fallback(monkeypatch):
    mod = _load_preflight_module()
    from sector_registry import SectorInstrument

    monkeypatch.setattr(mod, "load_priority_instruments", lambda *a, **k: [])
    monkeypatch.setattr(
        mod,
        "load_sector_instruments",
        lambda _sector: [SectorInstrument("ZZZ", "NSE")],
    )
    pairs = mod._resolve_sector_symbols(
        make_cfg(),
        "defence",
        priority_only=True,
        priority_top_n=10,
    )
    assert pairs == []


def test_check_sector_symbol_news_all_present(monkeypatch):
    mod = _load_preflight_module()
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        mod,
        "_resolve_sector_symbols",
        lambda _cfg, sector_id, priority_only=False, priority_top_n=None: [("HAL", "NSE")],
    )
    monkeypatch.setattr(
        mod,
        "get_recent_news_for_symbol",
        lambda *a, **k: [{"published_at": (now - timedelta(hours=1)).isoformat()}],
    )
    status = mod.check_sector_symbol_news(make_cfg(), "defence", now_utc=now)
    assert status is not None
    assert status["ok"] is True
    assert status["symbols_with_news"] == 1


def test_run_preflight_fail_open_exits_zero_on_failures(monkeypatch):
    mod = _load_preflight_module()
    monkeypatch.delenv("TITAN_NEWS_PREFLIGHT_STRICT", raising=False)
    monkeypatch.setattr(
        mod,
        "check_global_snapshot",
        lambda *a, **k: {"ok": False, "level": "warning", "message": "stale"},
    )
    monkeypatch.setattr(mod, "check_sector_symbol_news", lambda *a, **k: None)
    report = mod.run_preflight(make_cfg(), sector_id="")
    assert report["strict"] is False
    assert len(report["failures"]) == 1


def test_main_strict_mode_exits_one(monkeypatch):
    mod = _load_preflight_module()
    monkeypatch.setenv("TITAN_NEWS_PREFLIGHT_STRICT", "1")
    monkeypatch.setattr(mod.sys, "argv", ["preflight_news"])
    monkeypatch.setattr(mod, "prepare_news_script_config", lambda *a, **k: make_cfg())
    monkeypatch.setattr(
        mod,
        "run_preflight",
        lambda *a, **k: {
            "global": {"ok": False, "level": "error", "message": "missing"},
            "sector": None,
            "failures": [{"ok": False}],
            "strict": True,
        },
    )
    assert mod.main() == 1


def test_main_fail_open_exits_zero(monkeypatch):
    mod = _load_preflight_module()
    monkeypatch.delenv("TITAN_NEWS_PREFLIGHT_STRICT", raising=False)
    monkeypatch.setattr(mod.sys, "argv", ["preflight_news"])
    monkeypatch.setattr(mod, "prepare_news_script_config", lambda *a, **k: make_cfg())
    monkeypatch.setattr(
        mod,
        "run_preflight",
        lambda *a, **k: {
            "global": {"ok": False, "level": "warning", "message": "stale"},
            "sector": None,
            "failures": [{"ok": False}],
            "strict": False,
        },
    )
    assert mod.main() == 0
