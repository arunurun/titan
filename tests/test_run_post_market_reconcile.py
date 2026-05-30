from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_post_market_reconcile.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("run_post_market_reconcile", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_invokes_supabase_reconcile_runner(monkeypatch):
    mod = _load_script_module()
    mock_run = MagicMock(return_value={"scope": "all-stocks"})
    monkeypatch.setattr(mod, "run_reconcile_report", mock_run)
    monkeypatch.setattr(sys, "argv", ["prog", "--scope", "all-stocks", "--backfill-days", "2"])
    rc = mod.main()
    assert rc == 0
    mock_run.assert_called_once_with(
        sector=None,
        all_stocks=True,
        backfill_days=2,
    )


def test_main_backfill_only_skips_report_generation(monkeypatch):
    mod = _load_script_module()
    mock_run = MagicMock(return_value={"scope": "defence"})
    monkeypatch.setattr(mod, "run_reconcile_report", mock_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--scope", "sector", "--sector", "defence", "--backfill-only", "--backfill-days", "4"],
    )
    rc = mod.main()
    assert rc == 0
    mock_run.assert_called_once_with(
        sector="defence",
        all_stocks=False,
        backfill_days=4,
        generate_report=False,
        email_subject_prefix="Titan V12.0 reconcile backfill",
    )
