"""CLI wiring: --live failure notifies before re-raise."""

import json
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

    mock_run.assert_called_once_with(
        "defence", max_workers=None, max_symbols=None, digest=True
    )


def test_sector_passes_max_symbols(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--sector", "defence", "--sector-max-symbols", "5"],
    )
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "defence", max_workers=None, max_symbols=5, digest=True
    )


def test_sector_per_symbol_narrative(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--sector", "defence", "--sector-per-symbol-narrative"],
    )
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "defence", max_workers=None, max_symbols=None, digest=False
    )


def test_sector_passes_sector_workers(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(sys, "argv", ["prog", "--sector", "defence", "--sector-workers", "8"])
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "defence", max_workers=8, max_symbols=None, digest=True
    )


def test_sector_priority_only_passes_flags(monkeypatch):
    import main as main_mod
    import sector_audit

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--sector",
            "ai",
            "--sector-priority-only",
            "--sector-priority-top-n",
            "10",
        ],
    )
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "ai",
        max_workers=None,
        max_symbols=None,
        digest=True,
        priority_only=True,
        priority_top_n=10,
    )


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


def test_sector_passes_macro_snapshot(monkeypatch, tmp_path):
    import main as main_mod
    import sector_audit

    macro = tmp_path / "macro.json"
    macro.write_text(json.dumps({"gift_nifty_change_pct": -0.7, "india_vix": 19.0}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--sector", "defence", "--macro-json", str(macro)],
    )
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "defence",
        max_workers=None,
        max_symbols=None,
        digest=True,
        macro_snapshot={"gift_nifty_change_pct": -0.7, "india_vix": 19.0},
    )


def test_sector_passes_event_snapshot(monkeypatch, tmp_path):
    import main as main_mod
    import sector_audit

    events = tmp_path / "events.json"
    events.write_text(
        json.dumps({"events": [{"symbol": "HAL", "date": "2026-04-15", "type": "earnings"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--sector", "defence", "--events-json", str(events)],
    )
    mock_run = MagicMock()
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_run)

    main_mod.main()

    mock_run.assert_called_once_with(
        "defence",
        max_workers=None,
        max_symbols=None,
        digest=True,
        event_snapshot={"events": [{"symbol": "HAL", "date": "2026-04-15", "type": "earnings"}]},
    )


def test_protocol_run_invokes_window_runner(monkeypatch):
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["prog", "--protocol-run", "--protocol-window", "mid"])
    mock_runner = MagicMock()
    monkeypatch.setattr(main_mod, "run_protocol_window", mock_runner)

    main_mod.main()

    mock_runner.assert_called_once_with(
        window="mid",
        clusters=(),
        strict_window=False,
        macro_snapshot=None,
        event_snapshot=None,
        max_workers=None,
        max_symbols=None,
    )


def test_all_sectors_priority_invokes_runner(monkeypatch):
    import main as main_mod

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--all-sectors",
            "--sector-priority-only",
            "--sector-priority-top-n",
            "5",
            "--sector-workers",
            "2",
        ],
    )
    mock_runner = MagicMock()
    monkeypatch.setattr(main_mod, "run_all_sectors", mock_runner)

    main_mod.main()

    mock_runner.assert_called_once_with(
        max_workers=2,
        all_sector_workers=None,
        max_symbols=None,
        digest=True,
        exclude_sectors=("unknown", "non_equity"),
        macro_snapshot=None,
        event_snapshot=None,
        priority_only=True,
        priority_top_n=5,
    )


def test_all_sectors_invokes_parallel_runner(monkeypatch):
    import main as main_mod

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--all-sectors",
            "--all-sector-workers",
            "3",
            "--sector-workers",
            "4",
            "--sector-max-symbols",
            "25",
        ],
    )
    mock_runner = MagicMock()
    monkeypatch.setattr(main_mod, "run_all_sectors", mock_runner)

    main_mod.main()

    mock_runner.assert_called_once_with(
        max_workers=4,
        all_sector_workers=3,
        max_symbols=25,
        digest=True,
        exclude_sectors=("unknown", "non_equity"),
        macro_snapshot=None,
        event_snapshot=None,
        priority_only=False,
        priority_top_n=None,
    )


def test_run_all_sectors_default_sends_per_sector_email(monkeypatch):
    import main as main_mod
    import sector_audit
    import sector_registry

    monkeypatch.delenv("TITAN_ALL_SECTORS_SINGLE_DIGEST", raising=False)
    monkeypatch.setattr(main_mod, "send_success_post_email", MagicMock())
    monkeypatch.setattr(sector_registry, "list_active_sector_ids", lambda include_unknown=False: ["defence", "energy"])

    def _fake_run_sector_live(sector_id, **kwargs):
        assert kwargs["send_email"] is True
        return f"Digest {sector_id}"

    monkeypatch.setattr(sector_audit, "run_sector_live", _fake_run_sector_live)

    main_mod.run_all_sectors(
        max_workers=2,
        all_sector_workers=2,
        max_symbols=None,
        digest=True,
        exclude_sectors=(),
        macro_snapshot=None,
        event_snapshot=None,
        priority_only=False,
        priority_top_n=None,
    )
    main_mod.send_success_post_email.assert_not_called()


def test_run_all_sectors_single_digest_env(monkeypatch):
    import main as main_mod
    import sector_audit
    import sector_registry

    monkeypatch.setenv("TITAN_ALL_SECTORS_SINGLE_DIGEST", "1")
    mock_email = MagicMock()
    monkeypatch.setattr(main_mod, "send_success_post_email", mock_email)
    monkeypatch.setattr(sector_registry, "list_active_sector_ids", lambda include_unknown=False: ["defence", "energy"])

    def _fake_run_sector_live(sector_id, **kwargs):
        assert kwargs["send_email"] is False
        return f"Digest for {sector_id}"

    monkeypatch.setattr(sector_audit, "run_sector_live", _fake_run_sector_live)

    main_mod.run_all_sectors(
        max_workers=2,
        all_sector_workers=2,
        max_symbols=None,
        digest=True,
        exclude_sectors=(),
        macro_snapshot=None,
        event_snapshot=None,
        priority_only=False,
        priority_top_n=None,
    )
    mock_email.assert_called_once()
    body = mock_email.call_args[0][0]
    assert "Titan all-sectors consolidated digest" in body
    assert "=== Sector: defence ===" in body
    assert "=== Sector: energy ===" in body


def test_portfolio_holdings_json_invokes_portfolio_analysis(monkeypatch, capsys):
    import main as main_mod
    import portfolio_analysis

    mock_email = MagicMock()
    monkeypatch.setattr(main_mod, "send_success_post_email", mock_email)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--portfolio-holdings-json",
            '[{"symbol":"HAL","quantity":10,"avg_buy_price":100}]',
            "--portfolio-max-positions",
            "10",
        ],
    )
    monkeypatch.setattr(
        portfolio_analysis,
        "analyze_portfolio_holdings",
        lambda holdings, max_positions=20: {
            "summary": {"analyzed_positions": len(holdings), "max_positions": max_positions},
            "rows": [],
            "top_candidates": [],
        },
    )
    main_mod.main()
    out = capsys.readouterr().out
    assert "workflow_portfolio_json" in out
    assert "Titan portfolio digest" in out
    assert "Analyzed: 1" in out
    mock_email.assert_called_once()
    assert mock_email.call_args[1].get("subject_prefix") == "Titan V12.0 portfolio"


def test_sector_run_takes_precedence_over_invalid_portfolio_json(monkeypatch):
    """portfolio_holdings_json was checked first; stray/non-JSON must not block sector runs."""
    import main as main_mod
    import sector_audit

    mock_sector = MagicMock(return_value="sector ok")
    monkeypatch.setattr(sector_audit, "run_sector_live", mock_sector)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--sector",
            "defence",
            "--sector-digest",
            "--portfolio-holdings-json",
            "{invalid json",  # would fail if portfolio path ran first
        ],
    )
    main_mod.main()
    mock_sector.assert_called_once()
