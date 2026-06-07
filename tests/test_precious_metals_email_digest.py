"""Tests for precious metals macro email digest formatter."""

from pathlib import Path

import pytest

from precious_metals_algo import (
    PreciousMetalsAlgo,
    format_precious_metals_digest_lines,
    generate_synthetic_pm_macro_series,
    load_pm_macro_series_from_csv,
)


@pytest.fixture
def silver_catch_up_result():
    algo = PreciousMetalsAlgo(z_window=20, z_threshold=1.0, sge_z_threshold=1.0)
    data = generate_synthetic_pm_macro_series(n=35)
    features = algo.generate_features(data)
    result = algo.execute_allocation_logic(features)
    return result, features


def test_formatter_contains_key_sections(silver_catch_up_result):
    result, features = silver_catch_up_result
    lines = format_precious_metals_digest_lines(
        result,
        features,
        "2026-06-05",
        book_value_inr=10_000_000,
    )
    text = "\n".join(lines)
    assert "--- Precious metals macro ---" in text
    assert "Read:" in text
    assert "▸ Macro backdrop (DXY)" in text
    assert "▸ Relative value (GSR)" in text
    assert "▸ Physical demand (SGE)" in text
    assert "▸ Recommended allocation" in text
    assert "▸ How we got here" in text
    assert "▸ One-line takeaway" in text
    assert "Gold:" in text
    assert "Silver:" in text
    assert "Cash:" in text
    assert "Conviction:" in text


def test_formatter_read_line_and_allocation(silver_catch_up_result):
    result, features = silver_catch_up_result
    lines = format_precious_metals_digest_lines(
        result,
        features,
        "2026-06-05",
        book_value_inr=10_000_000,
    )
    text = "\n".join(lines)
    assert result["read_line"] in text
    assert f"{result['gold_pct']:.1f}%" in text
    assert f"{result['silver_pct']:.1f}%" in text
    assert "₹100L book" in text


def test_formatter_omits_usd_without_book_value(silver_catch_up_result):
    result, features = silver_catch_up_result
    lines = format_precious_metals_digest_lines(result, features, "2026-06-05", book_value_inr=None)
    text = "\n".join(lines)
    assert "$" not in text
    assert "Gold:" in text


def test_load_pm_macro_series_from_fixture():
    fixture = Path(__file__).parent / "fixtures" / "pm_macro_series.csv"
    data = load_pm_macro_series_from_csv(fixture)
    assert data is not None
    assert "GOLD" in data
    assert len(data["GOLD"]) >= 252


def test_load_pm_macro_series_fallback_to_example(tmp_path, monkeypatch):
    """When primary cache is missing, loader falls back to .example then fixture."""
    import precious_metals_algo as pm

    monkeypatch.delenv("TITAN_PM_MACRO_CSV", raising=False)
    monkeypatch.setattr(pm, "_DEFAULT_PM_MACRO_CSV", tmp_path / "missing.csv")
    monkeypatch.setattr(
        pm,
        "_PM_MACRO_CSV_FALLBACKS",
        (
            Path(__file__).parent.parent / "data" / "cache" / "pm_macro_series.csv.example",
            Path(__file__).parent / "fixtures" / "pm_macro_series.csv",
        ),
    )
    data = load_pm_macro_series_from_csv()
    assert data is not None
    assert len(data["GOLD"]) >= 252


def test_load_pm_macro_series_default_cache():
    cache = Path(__file__).parent.parent / "data" / "cache" / "pm_macro_series.csv"
    if not cache.is_file():
        pytest.skip("committed cache not present")
    data = load_pm_macro_series_from_csv(cache)
    assert data is not None
    assert len(data["GOLD"]) >= 252


def test_sample_formatted_output_snapshot(silver_catch_up_result):
    """Printable sample for manual review — asserts stable structure."""
    result, features = silver_catch_up_result
    lines = format_precious_metals_digest_lines(
        result,
        features,
        "2026-06-05",
        book_value_inr=10_000_000,
    )
    assert lines[0] == "--- Precious metals macro ---"
    assert lines[1] == "As of: 2026-06-05 (EOD)"
    assert lines[2].startswith("Read:")
    assert len(lines) >= 20
