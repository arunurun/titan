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
