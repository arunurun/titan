from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_market_audit_workflow_uses_630am_ist_weekdays() -> None:
    path = ROOT / ".github" / "workflows" / "market_audit.yml"
    text = path.read_text(encoding="utf-8")
    assert 'cron: "0 1 * * 1-5"' in text
    assert "Market-open guard (IST)" in text
    assert "--all-sectors --sector-workers 4 --exclude-sectors unknown,non_equity" in text


def test_run_titan_now_no_default_symbol_cap() -> None:
    path = ROOT / ".github" / "workflows" / "run_titan_now.yml"
    text = path.read_text(encoding="utf-8")
    assert 'default: ""' in text
    assert "- all_sectors" in text
