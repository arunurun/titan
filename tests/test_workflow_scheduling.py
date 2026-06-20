from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_run_titan_now_scheduled_630am_ist_weekdays() -> None:
    path = ROOT / ".github" / "workflows" / "run_titan_now.yml"
    text = path.read_text(encoding="utf-8")
    assert 'cron: "0 1 * * 1-5"' in text
    assert "Market-open guard (IST)" in text
    assert "mode=all_sectors" in text
    assert "titan_scope=full" in text
    assert "all_sector_workers=1" in text
    assert "--all-sectors --exclude-sectors unknown,non_equity" in text
    assert "breeze-token-updator" in text


def test_market_audit_is_deprecated_wrapper() -> None:
    path = ROOT / ".github" / "workflows" / "market_audit.yml"
    text = path.read_text(encoding="utf-8")
    assert "deprecated" in text.lower()
    assert "uses: ./.github/workflows/run_titan_now.yml" in text
    assert 'cron: "0 1 * * 1-5"' not in text


def test_run_titan_now_no_default_symbol_cap() -> None:
    path = ROOT / ".github" / "workflows" / "run_titan_now.yml"
    text = path.read_text(encoding="utf-8")
    assert 'default: ""' in text
    assert "- all_sectors" in text
    assert "all_sector_workers" in text
    assert 'default: "1"' in text


def test_daily_post_market_reconcile_workflow_schedule_and_script() -> None:
    path = ROOT / ".github" / "workflows" / "daily_post_market_reconcile.yml"
    text = path.read_text(encoding="utf-8")
    assert 'cron: "5 11 * * 1-5"' in text
    assert "Run post-market stock reconcile" in text
    assert "python scripts/run_post_market_reconcile.py $ARGS" in text
    assert "inject_breeze_session_from_supabase.py" not in text
    assert "BREEZE_API_KEY" not in text
