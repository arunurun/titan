"""CLI wiring: --live failure notifies before re-raise."""

import sys
from unittest.mock import MagicMock

import pytest


def test_live_failure_calls_send_failure_email(monkeypatch):
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["prog", "--live"])
    mock_fail = MagicMock()
    monkeypatch.setattr(main_mod, "send_failure_email", mock_fail)

    def raise_supabase() -> None:
        raise RuntimeError("[Supabase] no table")

    monkeypatch.setattr(main_mod, "run_live", raise_supabase)

    with pytest.raises(RuntimeError, match="no table"):
        main_mod.main()

    mock_fail.assert_called_once()
    summary = mock_fail.call_args[0][0]
    assert "[Supabase]" in summary
    detail = mock_fail.call_args[1].get("detail") or mock_fail.call_args.kwargs.get("detail")
    assert detail and "Traceback" in detail


def test_sector_invokes_run_sector_live(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(sys, "argv", ["prog", "--sector", "defence"])
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with("defence", max_workers=None)


def test_sector_passes_sector_workers(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(sys, "argv", ["prog", "--sector", "defence", "--sector-workers", "8"])
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with("defence", max_workers=8)


def test_sector_failure_calls_send_failure_email(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(sys, "argv", ["prog", "--sector", "defence"])
    mock_fail = MagicMock()
    monkeypatch.setattr(main_mod, "send_failure_email", mock_fail)
    monkeypatch.setattr(
        sector_audit,
        "run_sector_live",
        MagicMock(side_effect=RuntimeError("[Sector] all failed")),
    )

    with pytest.raises(RuntimeError, match="all failed"):
        main_mod.main()

    mock_fail.assert_called_once()
    assert "[Sector]" in mock_fail.call_args[0][0]
