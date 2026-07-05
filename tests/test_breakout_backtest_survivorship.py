from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breakout_backtest import (  # noqa: E402
    SURVIVORSHIP_HAIRCUT_PCT,
    _apply_survivorship_haircut,
    _range_to_months,
    build_report_markdown,
    load_historical_constituents,
)


def test_range_to_months():
    assert _range_to_months("3m") == 3.0
    assert _range_to_months("6m") == 6.0
    assert _range_to_months("1y") == 12.0


def test_load_historical_constituents_placeholder():
    assert load_historical_constituents("2024-01-01", "SMALL_CAP_100") == []


def test_apply_survivorship_haircut():
    summary = {"hit_rate_t5": 40.0, "hit_rate_t10": 20.0}
    adjusted = _apply_survivorship_haircut(summary)
    factor = 1.0 - SURVIVORSHIP_HAIRCUT_PCT / 100.0
    assert adjusted["expected_hit_rate_t5"] == round(40.0 * factor, 2)
    assert adjusted["expected_hit_rate_t10"] == round(20.0 * factor, 2)


def test_build_report_markdown_includes_survivorship_section():
    report = {
        "meta": {"range": "6m", "forward_horizons": [5, 10, 15]},
        "summary": {
            "total_signals": 10,
            "hit_rate_t5": 40.0,
            "coverage_t5": 10,
            "wins_t5": 4,
            "survivorship_haircut": _apply_survivorship_haircut({"hit_rate_t5": 40.0}),
            "survivorship_warning": "survivor bias warning",
        },
    }
    md = build_report_markdown(report)
    assert "Survivorship Haircut" in md
    assert "Expected real-world hit rate" in md
    assert "survivor bias warning" in md
