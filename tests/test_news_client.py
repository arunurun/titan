"""Unit tests for news_client fetch and normalization."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
import pytest

from news_client import (
    _finnhub_symbol,
    deduplicate_news_items,
    fetch_all_news_for_symbol,
    fetch_news_from_newsapi,
    normalize_news_item,
    reset_paid_api_circuit,
)


def test_normalize_news_item_newsapi():
    raw = {
        "title": "INFY profit jumps after beating estimates",
        "url": "https://example.com/infy-q4",
        "description": "Infosys reports strong quarterly results in India.",
        "publishedAt": "2026-05-30T10:00:00Z",
        "source": {"id": "reuters", "name": "Reuters"},
    }
    item = normalize_news_item(raw, "INFY", "NSE", "newsapi")
    assert item["symbol"] == "INFY"
    assert item["exchange"] == "NSE"
    assert item["title"] == raw["title"]
    assert item["url"] == raw["url"]
    assert item["source"] == "Reuters"
    assert float(item["relevance_score"]) > 0.8


def test_deduplicate_news_items():
    now = datetime.now(timezone.utc).isoformat()
    items = [
        {
            "title": "Same headline here",
            "url": "https://example.com/1",
            "published_at": now,
        },
        {
            "title": "Same headline here",
            "url": "https://example.com/2",
            "published_at": now,
        },
        {
            "title": "Different headline",
            "url": "https://example.com/3",
            "published_at": now,
        },
    ]
    out = deduplicate_news_items(items)
    assert len(out) == 2
    urls = {str(x["url"]) for x in out}
    assert "https://example.com/1" in urls
    assert "https://example.com/3" in urls
    assert "https://example.com/2" not in urls


def test_finnhub_symbol_maps_nse_bse():
    assert _finnhub_symbol("RELIANCE", "NSE") == "RELIANCE.NS"
    assert _finnhub_symbol("RELIANCE", "BSE") == "RELIANCE.BO"
    assert _finnhub_symbol("RELIANCE.NS", "NSE") == "RELIANCE.NS"


def test_fetch_news_from_newsapi_skips_without_key(monkeypatch):
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    assert fetch_news_from_newsapi("INFY", api_key="") == []


def test_fetch_news_from_newsapi_rate_limit_trips_circuit(monkeypatch):
    reset_paid_api_circuit()
    monkeypatch.delenv("TITAN_NEWS_SKIP_PAID_APIS", raising=False)

    class _BoomClient:
        def __init__(self, api_key):
            pass

        def get_everything(self, **kwargs):
            raise RuntimeError("rateLimited - developer plan")

    monkeypatch.setattr("newsapi.NewsApiClient", _BoomClient)
    first = fetch_news_from_newsapi("INFY", api_key="test-key")
    second = fetch_news_from_newsapi("TCS", api_key="test-key")
    assert first == []
    assert second == []
    reset_paid_api_circuit()


def test_fetch_all_news_for_symbol_fail_open_rss_only(monkeypatch):
    reset_paid_api_circuit()
    monkeypatch.setenv("TITAN_NEWS_SKIP_PAID_APIS", "1")
    recent = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        "news_client.fetch_news_from_rss_feeds",
        lambda **kwargs: [
            {
                "symbol": "HAL",
                "exchange": "NSE",
                "title": "HAL wins major order",
                "url": "https://example.com/hal-order",
                "source": "rss:moneycontrol",
                "published_at": recent,
                "summary": "HAL contract update",
                "relevance_score": 0.9,
            }
        ],
    )
    items = fetch_all_news_for_symbol("HAL", "NSE")
    assert items
    assert str(items[0].get("title") or "").startswith("HAL")
    reset_paid_api_circuit()
    monkeypatch.delenv("TITAN_NEWS_SKIP_PAID_APIS", raising=False)


def test_fetch_all_news_for_symbol_logs_source_summary(caplog, monkeypatch):
    reset_paid_api_circuit()
    monkeypatch.setenv("TITAN_NEWS_SKIP_PAID_APIS", "1")
    monkeypatch.setattr(
        "news_client.fetch_news_from_rss_feeds",
        lambda **kwargs: [
            {
                "symbol": "HAL",
                "exchange": "NSE",
                "title": "HAL wins major order",
                "url": "https://example.com/hal-order",
                "source": "rss:moneycontrol",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "summary": "HAL contract update",
                "relevance_score": 0.9,
            }
        ],
    )
    with caplog.at_level(logging.INFO, logger="news_client"):
        fetch_all_news_for_symbol("HAL", "NSE")
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "news_client"]
    assert any(
        "[news] HAL (NSE) — sources: rss=1 newsapi=skipped finnhub=skipped" in msg
        for msg in messages
    )
    reset_paid_api_circuit()
    monkeypatch.delenv("TITAN_NEWS_SKIP_PAID_APIS", raising=False)


@pytest.mark.skipif(
    not (os.environ.get("NEWSAPI_API_KEY") or "").strip(),
    reason="NEWSAPI_API_KEY not set",
)
def test_fetch_news_live_newsapi():
    items = fetch_news_from_newsapi("INFY", exchange="NSE", lookback_hours=48)
    assert items
    for item in items:
        assert item.get("symbol") == "INFY"
        assert str(item.get("title") or "").strip()
        assert str(item.get("url") or "").strip()
