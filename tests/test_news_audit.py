"""Unit tests for news_audit validation, correlation, and drivers (no live APIs)."""

from __future__ import annotations

from config_loader import TitanConfig
from news_audit import (
    correlate_news_with_price_move,
    extract_news_drivers,
    validate_news_payload,
)


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def test_validate_news_payload_valid_minimal():
    audit = {
        "recent_news": [{"title": "Headline", "source": "rss"}],
        "news_sentiment_score": 0.25,
        "news_sentiment_aggregate": "positive",
        "news_count": 1,
    }
    ok, issues = validate_news_payload(audit)
    assert ok is True
    assert issues == []


def test_validate_news_payload_invalid_cases():
    ok, issues = validate_news_payload("not-a-dict")  # type: ignore[arg-type]
    assert ok is False
    assert "audit_not_dict" in issues

    ok, issues = validate_news_payload(
        {
            "recent_news": "bad",
            "news_sentiment_score": 2.0,
            "news_sentiment_aggregate": "unknown",
            "news_count": -1,
        }
    )
    assert ok is False
    assert "recent_news_not_list" in issues
    assert "news_sentiment_score_out_of_range" in issues
    assert "news_sentiment_aggregate_invalid" in issues
    assert "news_count_negative" in issues

    ok, issues = validate_news_payload({"recent_news": [{"title": ""}]})
    assert ok is False
    assert any("missing_title" in i for i in issues)


def test_correlate_news_with_price_move_aligned():
    cfg = make_cfg()
    audit = {
        "news_sentiment_score": 0.4,
        "return_1d_pct": 1.2,
    }
    out = correlate_news_with_price_move(cfg, "HAL", audit)
    assert out["aligned"] is True
    assert out["contradiction_strength"] == 0.0
    assert out["possible_reason"] in ("", "insufficient_signal")


def test_correlate_news_with_price_move_contradiction():
    cfg = make_cfg()
    audit = {
        "news_sentiment_score": 0.5,
        "return_1d_pct": -2.0,
    }
    out = correlate_news_with_price_move(cfg, "HAL", audit)
    assert out["aligned"] is False
    assert out["contradiction_strength"] > 0.0
    assert out["possible_reason"] == "positive_headlines_with_negative_price"


def test_correlate_news_with_price_move_derives_sentiment_from_recent_news():
    cfg = make_cfg()
    audit = {
        "return_1d_pct": 1.5,
        "recent_news": [
            {"title": "Upbeat", "sentiment_score": 0.7, "relevance_score": 1.0},
            {"title": "Also up", "sentiment_score": 0.6, "relevance_score": 1.0},
        ],
    }
    out = correlate_news_with_price_move(cfg, "HAL", audit)
    assert out["aligned"] is True


def test_extract_news_drivers_ranks_by_impact_times_relevance():
    items = [
        {
            "title": "Low impact",
            "source": "a",
            "impact_level": "low",
            "relevance_score": 0.9,
            "published_at": "2026-01-01T00:00:00Z",
        },
        {
            "title": "High impact",
            "source": "b",
            "impact_level": "high",
            "relevance_score": 0.5,
            "published_at": "2026-01-02T00:00:00Z",
        },
    ]
    drivers = extract_news_drivers(items, limit=2)
    assert len(drivers) == 2
    assert drivers[0]["headline"] == "High impact"
    assert drivers[0]["impact_contribution"] >= drivers[1]["impact_contribution"]


def test_extract_news_drivers_skips_empty_titles():
    drivers = extract_news_drivers([{"title": "  ", "impact_level": "high"}], limit=3)
    assert drivers == []


def test_correlate_insufficient_signal_when_flat():
    cfg = make_cfg()
    audit = {"news_sentiment_score": 0.0, "return_1d_pct": 0.0}
    out = correlate_news_with_price_move(cfg, "HAL", audit)
    assert out["aligned"] is True
    assert out["possible_reason"] == "insufficient_signal"
