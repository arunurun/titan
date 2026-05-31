"""Unit tests for VADER sentiment and aggregation."""

from __future__ import annotations

import pytest

from news_sentiment import (
    aggregate_sentiment,
    compute_sentiment_vader,
    extract_event_type,
    sentiment_model_name,
)


def test_sentiment_vader_positive():
    result = compute_sentiment_vader(
        "Company profit jumps sharply after beating analyst estimates."
    )
    assert result["sentiment"] == "positive"
    assert float(result["score"]) > 0.3


def test_sentiment_vader_negative():
    result = compute_sentiment_vader(
        "Shares crash after regulatory probe and major loss warning."
    )
    assert result["sentiment"] == "negative"
    assert float(result["score"]) < -0.3


def test_sentiment_vader_neutral():
    result = compute_sentiment_vader(
        "Stock was broadly unchanged with mixed sector performance."
    )
    assert result["sentiment"] in ("neutral", "mixed")
    assert abs(float(result["score"])) < 0.2


def test_aggregate_sentiment_weighted():
    items = [
        {"sentiment_score": 0.8, "relevance_score": 0.9},
        {"sentiment_score": -0.8, "relevance_score": 0.1},
    ]
    agg = aggregate_sentiment(items, weight_by_relevance=True)
    assert agg["aggregate_sentiment"] == "positive"
    assert float(agg["aggregate_score"]) > 0.3


def test_aggregate_sentiment_equal_weight():
    items = [
        {"sentiment_score": 0.8, "relevance_score": 0.5},
        {"sentiment_score": -0.8, "relevance_score": 0.5},
    ]
    agg = aggregate_sentiment(items, weight_by_relevance=False)
    assert agg["aggregate_sentiment"] == "neutral"
    assert abs(float(agg["aggregate_score"])) < 0.1


def test_extract_event_type_earnings():
    assert extract_event_type("Q4 earnings beat expectations", "") == "earnings"


def test_sentiment_model_name_defaults_vader(monkeypatch):
    monkeypatch.delenv("TITAN_SENTIMENT_MODEL", raising=False)
    assert sentiment_model_name() == "vader"
