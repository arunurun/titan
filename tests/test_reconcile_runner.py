from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_reconcile_runner_builds_report_and_emails(monkeypatch):
    import reconcile_runner as rr

    monkeypatch.setattr(
        rr,
        "load_config",
        lambda: SimpleNamespace(supabase_url="https://example.supabase.co", supabase_key="service-key"),
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

    monkeypatch.setattr(
        rr,
        "load_config",
        lambda: SimpleNamespace(supabase_url="https://example.supabase.co", supabase_key="service-key"),
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
