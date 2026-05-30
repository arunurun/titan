"""News store and audit enrichment tests (mocked Supabase)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from config_loader import TitanConfig
from news_sentiment import aggregate_sentiment
from news_store import (
    cleanup_old_news,
    get_recent_news_for_symbol,
    get_symbol_news_snapshot,
    store_news_items,
)
from sector_registry import SectorInstrument


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


class _FakeExecute:
    def __init__(self, data: list[dict]):
        self.data = data


class _FakeQuery:
    """Minimal PostgREST-style chain returning fixed rows on execute()."""

    def __init__(self, data: list[dict]):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, *_a, **_k):
        return self

    def insert(self, *_a, **_k):
        return self

    def delete(self, *_a, **_k):
        return self

    def lt(self, *_a, **_k):
        return self

    def execute(self):
        return _FakeExecute(self._data)


def _mock_supabase_client(rows: list[dict] | None = None):
    client = MagicMock()
    client.table.return_value = _FakeQuery(rows or [])
    return client


@pytest.mark.skipif(
    not (os.environ.get("SUPABASE_URL") or "").strip()
    or not (os.environ.get("SUPABASE_KEY") or "").strip(),
    reason="SUPABASE_URL/SUPABASE_KEY not set (use mocked tests otherwise)",
)
def test_store_and_retrieve_news_live():
    from config_loader import load_config

    cfg = load_config()
    now = datetime.now(timezone.utc).isoformat()
    fixture = {
        "symbol": "TESTSTOCK",
        "exchange": "NSE",
        "title": "TESTSTOCK integration headline",
        "url": f"https://example.com/teststock-{int(datetime.now(timezone.utc).timestamp())}",
        "source": "pytest",
        "published_at": now,
        "summary": "integration fixture",
        "relevance_score": 0.9,
    }
    store_news_items(cfg, [fixture])
    rows = get_recent_news_for_symbol(cfg, "TESTSTOCK", lookback_hours=1, limit=5)
    assert any("TESTSTOCK integration headline" in str(r.get("title") or "") for r in rows)


def test_store_and_retrieve_news_mocked():
    cfg = make_cfg()
    now = datetime.now(timezone.utc).isoformat()
    stored_row = {
        "id": 99,
        "symbol": "TESTSTOCK",
        "exchange": "NSE",
        "title": "Mock headline for TESTSTOCK",
        "url": "https://example.com/mock-teststock",
        "published_at": now,
    }
    client = _mock_supabase_client([stored_row])
    with patch("news_store.create_client", return_value=client):
        result = store_news_items(
            cfg,
            [
                {
                    "symbol": "TESTSTOCK",
                    "exchange": "NSE",
                    "title": stored_row["title"],
                    "url": stored_row["url"],
                    "source": "pytest",
                    "published_at": now,
                }
            ],
        )
        assert result["inserted"] >= 1
        rows = get_recent_news_for_symbol(cfg, "TESTSTOCK", lookback_hours=1, limit=5)
    assert rows
    assert rows[0]["title"] == "Mock headline for TESTSTOCK"


def test_aggregate_sentiment_from_db_mocked():
    cfg = make_cfg()
    items = [
        {
            "title": "Positive headline",
            "sentiment_score": 0.6,
            "relevance_score": 0.8,
        },
        {
            "title": "Negative headline",
            "sentiment_score": -0.4,
            "relevance_score": 0.5,
        },
    ]
    client = _mock_supabase_client(items)
    with patch("news_store.create_client", return_value=client):
        rows = get_recent_news_for_symbol(cfg, "INFY", limit=10)
    assert rows
    agg = aggregate_sentiment(rows)
    assert "aggregate_sentiment" in agg
    assert "aggregate_score" in agg
    assert -1.0 <= float(agg["aggregate_score"]) <= 1.0


def test_get_symbol_news_snapshot_cache_hit_mocked(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setenv("TITAN_NEWS_SNAPSHOT_TTL_HOURS", "2")
    cached = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "symbol": "HAL",
        "news_count": 2,
        "aggregate_sentiment": "positive",
        "aggregate_score": 0.4,
    }
    client = _mock_supabase_client([cached])

    def _table(name: str):
        if name == "symbol_news_snapshots":
            return _FakeQuery([cached])
        return _FakeQuery([])

    client.table.side_effect = _table
    with patch("news_store.create_client", return_value=client):
        snap = get_symbol_news_snapshot(cfg, "HAL")
    assert snap["source"] == "cached"
    assert snap["symbol"] == "HAL"
    assert snap.get("fresh") is True


def test_cleanup_old_news_mocked():
    cfg = make_cfg()
    deleted_rows = [{"id": 1}, {"id": 2}]
    client = _mock_supabase_client(deleted_rows)
    with patch("news_store.create_client", return_value=client):
        result = cleanup_old_news(cfg, older_than_hours=72)
    assert result["deleted"] == 2


def test_enrich_audit_with_symbol_news_sets_fields(monkeypatch):
    from sector_audit import _enrich_audit_with_symbol_news

    cfg = make_cfg()
    inst = SectorInstrument("HAL", "NSE")
    audit: dict = {"symbol": "HAL", "return_1d_pct": 1.2}
    recent = [
        {
            "title": "HAL wins contract",
            "sentiment_score": 0.5,
            "relevance_score": 0.8,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    monkeypatch.setattr(
        "news_store.get_recent_news_for_symbol",
        lambda *a, **k: recent,
    )
    monkeypatch.setattr(
        "news_audit.compute_news_sentiment_trend",
        lambda *a, **k: {"trend": "flat", "trend_score": 0.0},
    )
    monkeypatch.setattr(
        "news_audit.correlate_news_with_price_move",
        lambda *a, **k: {
            "aligned": True,
            "contradiction_strength": 0.0,
            "possible_reason": "",
        },
    )
    _enrich_audit_with_symbol_news(cfg, inst, audit)
    assert audit.get("recent_news")
    assert audit["news_count"] == 1
    assert audit["news_sentiment_aggregate"] in ("positive", "negative", "neutral", "mixed")
    assert "news_error" not in audit


def test_enrich_audit_with_symbol_news_failure_nonblocking(monkeypatch):
    from sector_audit import _enrich_audit_with_symbol_news

    cfg = make_cfg()
    inst = SectorInstrument("HAL", "NSE")
    audit: dict = {"symbol": "HAL"}

    def _boom(*_a, **_k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr("news_store.get_recent_news_for_symbol", _boom)
    _enrich_audit_with_symbol_news(cfg, inst, audit)
    assert "news_error" in audit
    assert "supabase down" in str(audit["news_error"])
