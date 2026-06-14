from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_reconcile_runner_builds_report_and_emails(monkeypatch):
    import reconcile_runner as rr

    def _fake_load_config(*, require_breeze=True, require_gemini=True):
        assert require_breeze is False
        assert require_gemini is False
        return SimpleNamespace(supabase_url="https://example.supabase.co", supabase_key="service-key")

    monkeypatch.setattr(
        rr,
        "load_config",
        _fake_load_config,
    )
    monkeypatch.setattr(
        rr,
        "enrich_audits_with_stock_reconcile",
        lambda cfg, sector, all_stocks, audits: {
            "scope": "all-stocks",
            "as_of_trade_date": "2026-05-30",
            "symbol_count": 2,
            "coverage_next_day": 2,
            "coverage_next_week": 2,
            "news_attribution_efficacy": {"alignment_rate_pct": 66.7},
            "per_symbol": {},
        },
    )
    monkeypatch.setattr(rr, "build_reconcile_digest_lines", lambda summary: ["digest-line-1", "digest-line-2"])
    mock_email = MagicMock(return_value=True)
    monkeypatch.setattr(rr, "send_success_post_email", mock_email)
    out = rr.run_reconcile_report(sector=None, all_stocks=True, backfill_days=0)
    assert out["scope"] == "all-stocks"
    assert "digest-line-1" in out["digest_text"]
    assert "Supabase-only" in out["digest_text"]
    mock_email.assert_called_once()
    assert "digest-line-2" in mock_email.call_args[0][0]


def test_reconcile_runner_does_not_invoke_breeze(monkeypatch):
    import breeze_client
    import reconcile_runner as rr

    def _fake_load_config(*, require_breeze=True, require_gemini=True):
        assert require_breeze is False
        assert require_gemini is False
        return SimpleNamespace(supabase_url="https://example.supabase.co", supabase_key="service-key")

    monkeypatch.setattr(
        rr,
        "load_config",
        _fake_load_config,
    )
    monkeypatch.setattr(
        rr,
        "enrich_audits_with_stock_reconcile",
        lambda cfg, sector, all_stocks, audits: {
            "scope": "sector",
            "as_of_trade_date": "2026-05-30",
            "symbol_count": 1,
            "coverage_next_day": 1,
            "coverage_next_week": 1,
            "news_attribution_efficacy": {"alignment_rate_pct": 100.0},
            "per_symbol": {},
        },
    )
    monkeypatch.setattr(rr, "build_reconcile_digest_lines", lambda summary: ["decision-efficacy"])
    monkeypatch.setattr(rr, "send_success_post_email", lambda body, subject_prefix=None: True)
    mock_breeze = MagicMock(side_effect=AssertionError("Breeze must not be called"))
    monkeypatch.setattr(breeze_client, "create_breeze_session", mock_breeze)
    rr.run_reconcile_report(sector="defence", all_stocks=False, backfill_days=0)
    mock_breeze.assert_not_called()


def test_reconcile_runner_passes_without_breeze_or_gemini(monkeypatch, tmp_path):
    import reconcile_runner as rr
    from config_loader import load_config as real_load_config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_KEY=service-key\n",
        encoding="utf-8",
    )

    for k in list(os.environ.keys()):
        if k.startswith("BREEZE") or k.startswith("GEMINI") or k.startswith("SUPABASE"):
            monkeypatch.delenv(k, raising=False)

    def _real_loader_with_tmp_env(*, require_breeze=True, require_gemini=True):
        return real_load_config(
            env_file,
            require_breeze=require_breeze,
            require_gemini=require_gemini,
        )

    monkeypatch.setattr(rr, "load_config", _real_loader_with_tmp_env)
    out = rr.run_reconcile_report(
        sector=None,
        all_stocks=True,
        backfill_days=0,
        generate_report=False,
    )
    assert out["scope"] == "all-stocks"
    assert out["summary"] == {}
    assert out["digest_text"] == ""


def test_reconcile_runner_persists_forward_outcomes_when_enabled(monkeypatch):
    import reconcile_runner as rr

    monkeypatch.setenv("TITAN_FORWARD_OUTCOMES_PERSIST", "1")

    def _fake_load_config(*, require_breeze=True, require_gemini=True):
        return SimpleNamespace(supabase_url="https://example.supabase.co", supabase_key="service-key")

    monkeypatch.setattr(rr, "load_config", _fake_load_config)
    monkeypatch.setattr(
        rr,
        "enrich_audits_with_stock_reconcile",
        lambda cfg, sector, all_stocks, audits: {"scope": "defence", "symbol_count": 0},
    )
    monkeypatch.setattr(rr, "build_reconcile_digest_lines", lambda summary: [])
    monkeypatch.setattr(rr, "send_success_post_email", lambda body, subject_prefix=None: True)
    mock_persist = MagicMock(return_value={"enabled": True, "updated": 3, "candidates": 5, "scope": "defence"})
    monkeypatch.setattr(rr, "persist_forward_outcomes", mock_persist)

    out = rr.run_reconcile_report(sector="defence", all_stocks=False, backfill_days=0, generate_report=True)
    mock_persist.assert_called_once()
    assert out["forward_outcomes"]["updated"] == 3
