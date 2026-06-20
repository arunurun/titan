TITAN_NEWS_INTEGRATION_PLAN.md

md
# TITAN V12.0 News Integration & Correlation Feature Specification

Complete implementation guide for integrating real-time financial news aggregation, sentiment analysis, and correlation with TITAN's stock analysis engine.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Database Schema](#database-schema)
4. [Core Modules](#core-modules)
5. [Integration Points](#integration-points)
6. [Scheduler & CI/CD](#scheduler--cicd)
7. [Testing Strategy](#testing-strategy)
8. [Deployment Checklist](#deployment-checklist)
9. [Appendices](#appendices)

---

## Overview

This document provides detailed implementation instructions for integrating real-time financial news aggregation and correlation with TITAN's existing stock analysis engine. The feature will:

1. **Fetch news** from multiple sources (APIs, RSS feeds, web scraping)
2. **Compute sentiment** and relevance scores using NLP
3. **Store in Supabase** for persistence and deduplication
4. **Integrate into audit payloads** to enrich Gemini narratives
5. **Measure news impact** on intent scores and technical signals
6. **Flag contradictions** between sentiment and price movements
7. **Correlate events** (earnings, regulatory) with market reactions

**Key Benefits:**
- Richer context for Gemini-powered narratives
- Early detection of news-driven market dislocations
- Improved signal quality through sentiment-technical alignment checks
- Foundation for event-driven trading strategies

**Target Users:**
- Cursor AI coding assistant (for implementation)
- TITAN V12.0 analysts (for news-enriched audit narratives)
- GitHub Actions workflows (for automated news fetching)

---

## Architecture

### Component Stack

External Sources (NewsAPI, Finnhub, RSS, Web) ↓ news_client.py (Fetcher & Normalization) ↓ news_sentiment.py (VADER/FinBERT Scoring) ↓ news_store.py (Supabase CRUD + Dedup) ↓ news_audit.py (Quality Checks & Correlation) ↓ sector_audit.py (Audit Enrichment Hook) ↓ brain.py (Gemini Narrative Generation) ↓ Email / X / LinkedIn Output

Code

### Data Flow Diagram

┌─────────────────────────────────────────────────────────────┐ │ News Fetcher Thread (Scheduled: Every 2 Hours, 09:00-17:00) │ └──────────────┬────────────────────────────────────────────────┘ │ ├─→ NewsAPI (100 articles/day free) ├─→ Finnhub (real-time, Indian focus) └─→ RSS Feeds (Bloomberg, Reuters, Moneycontrol) │ ↓ ┌──────────────────┐ │ Normalize Items │ │ (URL, Title, Src)│ └────────┬─────────┘ │ ↓ ┌──────────────────┐ │ Deduplicate │ │ (URL UNIQUE) │ └────────┬─────────┘ │ ↓ ┌──────────────────┐ │ Compute Sentiment│ │ (VADER/FinBERT) │ └────────┬─────────┘ │ ↓ ┌────────────────────────┐ │ Store in Supabase │ │ - news_feed │ │ - news_sentiment_cache │ │ - global_news_snapshots│ └────────────────────────┘

┌─────────────────────────────────────────────────────────────┐ │ Sector Analysis Run (Daily/On-Demand: main.py --all-sectors)│ └──────────────┬────────────────────────────────────────────────┘ │ ↓ ┌──────────────────────────┐ │ For Each Symbol: │ │ 1. Fetch Technical Data │ │ 2. Fetch Cached News │ │ 3. Compute Z-Score, VPR │ │ 4. Aggregate News │ │ Sentiment │ │ 5. Check Alignment │ │ (Sentiment vs Price) │ └────────┬─────────────────┘ │ ↓ ┌────────────────────────────┐ │ Enrich Audit Dict: │ │ - recent_news: [] │ │ - news_sentiment_aggregate │ │ - news_sentiment_score │ │ - news_sentiment_trend │ │ - news_price_alignment │ │ - news_price_contradiction │ └────────┬───────────────────┘ │ ↓ ┌──────────────────────────────┐ │ Call Gemini with Rich Audit │ │ (JSON payload includes news) │ └────────┬─────────────────────┘ │ ↓ ┌──────────────────────────────────────┐ │ Generate Narrative: │ │ - Mention top news drivers │ │ - Flag sentiment/price contradictions│ │ - Note event catalysts │ └────────┬─────────────────────────────┘ │ ↓ ┌──────────────────────────────┐ │ Output: │ │ - Email Digest │ │ - X/LinkedIn Post │ │ - Supabase Analytics Store │ └──────────────────────────────┘

Code

### Integration Points with Existing TITAN

| Component | Modification | Impact |
|-----------|--------------|--------|
| `main.py` | Add `--news-refresh` flag; wire news into `run_sector_live()` | Optional; backward-compatible |
| `sector_audit.py` | Enrichment hook after technical audit | Non-blocking; news failures don't halt analysis |
| `brain.py` | Include news in JSON payload to Gemini; update system prompt | Automatic via JSON serialization |
| `analysis_store.py` | Store `news_correlation` field in `symbol_daily_features` | Enables post-hoc analysis of news impact |
| CI/CD (GitHub Actions) | Add `news_fetch.yml` workflow on separate schedule | Independent from run_titan_now.yml |

---

## Database Schema

All tables are deployed to Supabase PostgreSQL. Run migrations via:

```sql
-- Run in Supabase SQL Editor or via supabase-cli
psql -h <SUPABASE_HOST> -U postgres -d postgres -f sql/create_news_tables.sql
Table: news_feed
Core news items with deduplication and sentiment scores.

SQL
CREATE TABLE news_feed (
    id BIGSERIAL PRIMARY KEY,
    
    -- Symbol & Exchange Mapping
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(10) NOT NULL DEFAULT 'NSE',
    
    -- News Content
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source VARCHAR(50) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE NOT NULL,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Sentiment Scores
    sentiment VARCHAR(20) NOT NULL DEFAULT 'neutral',
    sentiment_score FLOAT DEFAULT 0.0,
    sentiment_model VARCHAR(50) DEFAULT 'vader',
    
    -- Relevance & Deduplication
    relevance_score FLOAT DEFAULT 0.5,
    is_duplicate BOOLEAN DEFAULT FALSE,
    duplicate_of_id BIGINT REFERENCES news_feed(id),
    
    -- Metadata
    summary TEXT,
    event_type VARCHAR(50),
    impact_level VARCHAR(20) DEFAULT 'medium',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_news_symbol_published ON news_feed(symbol, published_at DESC);
CREATE INDEX idx_news_source ON news_feed(source);
CREATE INDEX idx_news_fetched ON news_feed(fetched_at DESC);
CREATE INDEX idx_news_url ON news_feed(url);
CREATE INDEX idx_news_exchange ON news_feed(exchange);
Columns Explained:

symbol: NSE/BSE symbol (e.g., 'RELIANCE')
exchange: 'NSE' or 'BSE'
url: Unique identifier; prevents duplicate ingestion
source: Origin API ('newsapi', 'finnhub', 'rss:moneycontrol', etc.)
sentiment: 'positive', 'negative', 'neutral', 'mixed'
sentiment_score: -1.0 (bearish) to +1.0 (bullish)
sentiment_model: Which model computed the score ('vader' for VADER, 'finbert' for FinBERT)
impact_level: 'high' (earnings, regulatory), 'medium' (typical), 'low' (background noise)
event_type: Classification ('earnings', 'acquisition', 'regulatory', 'dividend', 'general')
relevance_score: 0.0-1.0; keyword match quality to symbol
Table: news_sentiment_cache
Caches expensive sentiment computations to avoid re-scoring.

SQL
CREATE TABLE news_sentiment_cache (
    id BIGSERIAL PRIMARY KEY,
    news_id BIGINT UNIQUE NOT NULL REFERENCES news_feed(id) ON DELETE CASCADE,
    
    -- Hash Keys
    title_hash VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64),
    
    -- Cached Scores
    sentiment VARCHAR(20),
    sentiment_score FLOAT,
    confidence FLOAT,
    
    -- Model Metadata
    model_used VARCHAR(50),
    computation_time_ms FLOAT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_sentiment_cache_hash ON news_sentiment_cache(title_hash);
CREATE INDEX idx_sentiment_cache_news ON news_sentiment_cache(news_id);
Purpose: If a news item's title/content appears again, retrieve cached sentiment instead of recomputing.

Table: global_news_snapshots
Pre-aggregated snapshots for quick lookups during sector runs.

SQL
CREATE TABLE global_news_snapshots (
    id BIGSERIAL PRIMARY KEY,
    
    -- Snapshot Metadata
    snapshot_at TIMESTAMP WITH TIME ZONE NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    
    -- Aggregates
    news_count INT DEFAULT 0,
    recent_news_items JSONB,
    aggregate_sentiment VARCHAR(20),
    aggregate_score FLOAT,
    sentiment_trend FLOAT,
    
    -- Top Drivers & Events
    top_drivers JSONB,
    event_alerts JSONB,
    
    -- Cache Control
    ttl_seconds INT DEFAULT 7200,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_snapshots_symbol_time ON global_news_snapshots(symbol, snapshot_at DESC);
Example Snapshot Payload:

JSON
{
  "symbol": "RELIANCE",
  "snapshot_at": "2026-05-30T12:00:00Z",
  "news_count": 5,
  "aggregate_sentiment": "positive",
  "aggregate_score": 0.72,
  "sentiment_trend": "strengthening",
  "recent_news_items": [
    {
      "title": "RELIANCE Q4 profit up 25% YoY",
      "source": "moneycontrol",
      "published_at": "2026-05-30T08:30:00Z",
      "sentiment": "positive",
      "sentiment_score": 0.85
    }
  ],
  "top_drivers": [
    {"headline": "Earnings beat estimates", "impact_contribution": 0.35}
  ]
}
Table: symbol_daily_features (Extended)
Add news correlation fields to existing symbol_daily_features table (if using analysis_store).

SQL
ALTER TABLE symbol_daily_features ADD COLUMN IF NOT EXISTS (
    news_correlation JSONB,
    news_sentiment_aggregate VARCHAR(20),
    news_sentiment_score FLOAT,
    news_sentiment_trend VARCHAR(20),
    news_count INT
);

CREATE INDEX idx_symbol_features_news_sentiment ON symbol_daily_features(symbol, news_sentiment_aggregate);
Structure of news_correlation JSONB:

JSON
{
  "driver": "Reliance profit beat",
  "affected_metric": "momentum5d",
  "affected_theme": "energy",
  "direction": "tailwind",
  "confidence": 0.82,
  "evidence": {
    "top_headlines": {
      "stock": [...],
      "local": [...],
      "global": [...]
    },
    "net_news_impact_score": 0.35
  },
  "driver_source": "stock",
  "stock_news_fetched_count": 3,
  "stock_news_coverage": "fetched",
  "available": true
}
Core Modules
Module 1: src/news_client.py — News Fetcher
Purpose: Aggregate news from multiple sources, normalize formats, deduplicate.

Key Functions:

Python
def fetch_news_from_newsapi(
    symbol: str,
    exchange: str = "NSE",
    lookback_hours: int = 24,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch from NewsAPI (free tier: up to 100 articles/day).
    Returns normalized news items with metadata.
    """

def fetch_news_from_finnhub(
    symbol: str,
    api_key: str | None = None,
    lookback_hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Fetch from Finnhub (better Indian market coverage, real-time).
    """

def fetch_news_from_rss_feeds(
    feeds: list[str] | None = None,
    symbol: str = "",
    lookback_hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Parse RSS/Atom feeds (Bloomberg, Reuters, CNBC-TV18, Moneycontrol, ET Markets).
    """

def fetch_all_news_for_symbol(
    symbol: str,
    exchange: str = "NSE",
    cfg: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Orchestrator: Fetch from all sources, deduplicate, rank by relevance.
    """

def normalize_news_item(
    raw: dict[str, Any],
    symbol: str,
    exchange: str,
    source: str,
) -> dict[str, Any]:
    """
    Convert from source-specific format to standardized dict.
    """

def deduplicate_news_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Remove duplicates by URL and title hash.
    """
Dependencies to Add to requirements.txt:

Code
newsapi-python>=0.1.1
finnhub-python>=1.2.14
feedparser>=6.0.10
requests>=2.32.0
Environment Variables (to add to .env.example):

Code
NEWSAPI_API_KEY=
FINNHUB_API_KEY=
TITAN_NEWS_FEEDS=https://feeds.moneycontrol.com/...,https://feeds.reuters.com/...
TITAN_NEWS_MAX_AGE_HOURS=36
TITAN_NEWS_SNAPSHOT_TTL_HOURS=2
TITAN_NEWS_SNAPSHOT_TABLE=global_news_snapshots
TITAN_NEWS_FETCH_LIMIT=40
Module 2: src/news_sentiment.py — Sentiment Analysis
Purpose: Compute sentiment scores using VADER (fast, financial-aware) and optional ML models.

Key Functions:

Python
def compute_sentiment_vader(text: str) -> dict[str, Any]:
    """
    VADER (Valence Aware Dictionary and sEntiment Reasoner).
    Fast, no network, handles financial text well.
    """

def compute_sentiment_transformers(
    text: str,
    model_name: str = "ProsusAI/finbert",
) -> dict[str, Any]:
    """
    FinBERT or DistilBERT-Financial: higher accuracy but ~0.5s per item.
    """

def aggregate_sentiment(
    items: list[dict[str, Any]],
    weight_by_relevance: bool = True,
) -> dict[str, Any]:
    """
    Weighted average of sentiment scores.
    """

def extract_event_type(title: str, text: str = "") -> str:
    """
    Classify news into event buckets.
    """

def extract_company_entities(text: str) -> list[str]:
    """
    Extract company names / tickers mentioned in news body.
    """
Dependencies to Add:

Code
nltk>=3.8.1
transformers>=4.40.0
torch>=2.0.0
Module 3: src/news_store.py — Supabase Persistence
Purpose: Store, deduplicate, and retrieve news from Supabase.

Key Functions:

Python
def store_news_items(
    cfg: Any,
    items: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Insert news into news_feed table with deduplication.
    """

def get_recent_news_for_symbol(
    cfg: Any,
    symbol: str,
    exchange: str = "NSE",
    lookback_hours: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Retrieve cached news for a symbol from news_feed table.
    """

def get_symbol_news_snapshot(
    cfg: Any,
    symbol: str,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Get or create a cached snapshot from global_news_snapshots.
    """

def mark_news_as_duplicate(
    cfg: Any,
    news_id: int,
    duplicate_of_id: int,
) -> None:
    """Mark a news item as duplicate and link to original."""

def cleanup_old_news(
    cfg: Any,
    older_than_hours: int = 72,
) -> dict[str, int]:
    """
    Prune old news (keep last N hours by default 72 = 3 days).
    """
Module 4: src/news_audit.py — Quality Checks & Correlation
Purpose: Validate news data and compute correlation metrics with technical signals.

Key Functions:

Python
def validate_news_payload(
    audit: dict[str, Any],
) -> tuple[bool, list[str]]:
    """
    Quality checks on enriched audit dict.
    """

def compute_news_sentiment_trend(
    cfg: Any,
    symbol: str,
    window_hours: int = 24,
) -> dict[str, Any]:
    """
    Compute sentiment momentum.
    """

def correlate_news_with_price_move(
    cfg: Any,
    symbol: str,
    audit_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Compare sentiment direction with 1-day price change.
    """

def extract_news_drivers(
    items: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Rank news by impact_level × relevance_score.
    """
Integration Points
Integration 1: Modify src/sector_audit.py
Add news enrichment after building technical audit dicts. Around line 2269 where _refresh_symbol_scoring_outputs() is called:

Python
# ADD THIS BLOCK AFTER _refresh_symbol_scoring_outputs(audit):
# ==================== NEWS ENRICHMENT START ====================

try:
    from news_store import get_recent_news_for_symbol
    from news_sentiment import aggregate_sentiment
    from news_audit import compute_news_sentiment_trend, correlate_news_with_price_move
    
    symbol = inst.symbol
    exchange = inst.exchange
    
    # Fetch recent news for this symbol
    lookback_hours = int(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", 36))
    driver_limit = int(os.environ.get("TITAN_NEWS_DRIVER_LIMIT", 3))
    
    recent_news = get_recent_news_for_symbol(
        cfg,
        symbol,
        exchange,
        lookback_hours=lookback_hours,
        limit=driver_limit * 2,
    )
    
    if recent_news:
        # Store top drivers in audit
        audit["recent_news"] = recent_news[:driver_limit]
        
        # Aggregate sentiment
        sentiment_agg = aggregate_sentiment(recent_news)
        audit["news_sentiment_aggregate"] = sentiment_agg["aggregate_sentiment"]
        audit["news_sentiment_score"] = sentiment_agg["aggregate_score"]
        audit["news_count"] = len(recent_news)
        
        # Compute trend
        trend = compute_news_sentiment_trend(cfg, symbol)
        audit["news_sentiment_trend"] = trend["trend"]
        audit["news_sentiment_trend_score"] = trend["trend_score"]
        
        # Check alignment with price movement
        corr = correlate_news_with_price_move(cfg, symbol, audit)
        audit["news_price_alignment"] = corr["aligned"]
        if not corr["aligned"]:
            audit["news_price_contradiction"] = corr["contradiction_strength"]
            audit["news_price_contradiction_reason"] = corr["possible_reason"]
    else:
        # No news; set defaults
        audit["recent_news"] = []
        audit["news_count"] = 0
        audit["news_sentiment_aggregate"] = "neutral"
        audit["news_sentiment_score"] = 0.0
        
except Exception as e:
    logger.warning(f"News enrichment failed for {inst.symbol}: {e}")
    audit["news_error"] = str(e)

# ==================== NEWS ENRICHMENT END ====================
Integration 2: Modify src/brain.py
Update system prompt to include news context:

Python
# MODIFY AROUND LINE 40 IN brain.py

TITAN_V12_SYSTEM_INSTRUCTION = """You are Titan V12.0 Forensic Analyst with News Intelligence.

Protocol:
- Analyze market structure, positioning, risk context AND recent financial news.
- When news context is available, cite top drivers (headline, source, recency).
- Flag sentiment/price contradictions: e.g., "positive earnings but stock down" → investigate.
- Highlight event-driven catalysts: earnings beats, regulatory approvals, M&A announcements.
- Mention news-technical alignment: "Technical strength supported by positive news" or "Technicals diverge from bearish headlines".
- Never give investment advice, price targets, entries, exits, or trading imperatives.
- Never use the words Buy, Sell, Target, SL, Stop Loss.
- Output a single concise post suitable for X/LinkedIn (plain text, <280 characters when standalone).
- Run mental policy compliance check before responding."""

# The audit_data JSON payload already includes news fields:
# - recent_news (array)
# - news_sentiment_aggregate
# - news_sentiment_score
# - news_sentiment_trend
# - news_price_alignment
# - news_price_contradiction
#
# Gemini will automatically incorporate this into the narrative generation.
Integration 3: Modify src/sector_priority.py (Optional)
Use news sentiment to influence priority ranking:

Python
# AROUND LINE 300-350 WHERE PRIORITY SCORES ARE COMPUTED

# If implementing news blending for priority:
news_sentiment_score = audit.get("news_sentiment_score", 0.0)
news_weight = float(os.environ.get("TITAN_NEWS_BLEND_WEIGHT", 3.5))
news_cap = float(os.environ.get("TITAN_NEWS_BLEND_CAP", 3))

# Clip news contribution
news_contribution = max(-news_cap, min(news_cap, news_sentiment_score * news_weight))

# Add to composite priority score (10% news influence)
composite_score = (technical_score * 0.9) + (news_contribution * 0.1)

# Update audit with news-blended intent if desired
audit["intent_score_news_blended"] = composite_score
Scheduler & CI/CD
GitHub Actions Workflow: .github/workflows/news_fetch.yml
YAML
name: Fetch News and Update Cache

on:
  schedule:
    # Every 2 hours during market hours (09:00-17:00 IST = 03:30-11:30 UTC)
    # Run Mon-Fri
    - cron: '30 3,5,7,9,11 * * 1-5'
  workflow_dispatch:  # Manual trigger

jobs:
  fetch-news:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install newsapi-python finnhub-python feedparser nltk
      
      - name: Fetch and cache news
        env:
          NEWSAPI_API_KEY: ${{ secrets.NEWSAPI_API_KEY }}
          FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
          TITAN_NEWS_FEEDS: ${{ secrets.TITAN_NEWS_FEEDS }}
          TITAN_NEWS_MAX_AGE_HOURS: 36
          TITAN_NEWS_FETCH_LIMIT: 40
        run: |
          python scripts/fetch_news_batch.py --sectors all --refresh-snapshots
      
      - name: Cleanup old news (>72h)
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
        run: |
          python scripts/cleanup_news.py --older-than-hours 72
      
      - name: Report status
        if: always()
        run: |
          echo "News fetch workflow completed"
          echo "Check Supabase for: news_feed, global_news_snapshots tables"
Helper Script: scripts/fetch_news_batch.py
Python
#!/usr/bin/env python3
"""
Batch fetch news for all active sectors and update Supabase cache.

Usage:
  python scripts/fetch_news_batch.py --sectors all --refresh-snapshots
  python scripts/fetch_news_batch.py --sectors defence,banking
"""

from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config_loader import load_config
from sector_registry import list_active_sector_ids, load_sector_instruments
from news_client import fetch_all_news_for_symbol
from news_store import store_news_items, get_symbol_news_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def fetch_and_store_for_symbol(cfg, symbol: str, exchange: str, refresh_snapshots: bool) -> dict:
    """Fetch and store news for a single symbol."""
    try:
        news_items = fetch_all_news_for_symbol(symbol, exchange, cfg)
        if not news_items:
            return {"symbol": symbol, "fetched": 0, "stored": 0, "error": None}
        
        # Store in Supabase
        result = store_news_items(cfg, news_items)
        
        # Refresh snapshot if requested
        if refresh_snapshots and result.get("inserted", 0) > 0:
            get_symbol_news_snapshot(cfg, symbol, force_refresh=True)
        
        return {
            "symbol": symbol,
            "fetched": len(news_items),
            "stored": result.get("inserted", 0),
            "duplicates": result.get("duplicates_skipped", 0),
            "error": None,
        }
    except Exception as e:
        logger.error(f"Failed for {symbol}: {e}")
        return {"symbol": symbol, "fetched": 0, "stored": 0, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Batch fetch and cache news")
    parser.add_argument(
        "--sectors",
        default="all",
        help="'all' or comma-separated sector IDs (e.g., 'defence,banking')"
    )
    parser.add_argument(
        "--refresh-snapshots",
        action="store_true",
        help="Force refresh snapshot cache"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel workers (default: 4)"
    )
    args = parser.parse_args()
    
    cfg = load_config()
    
    # Determine sectors
    if args.sectors == "all":
        sector_ids = list_active_sector_ids(include_unknown=False)
    else:
        sector_ids = [s.strip() for s in args.sectors.split(",")]
    
    logger.info(f"Starting news fetch for sectors: {', '.join(sector_ids)}")
    
    # Collect all symbols
    symbol_pairs = []
    for sector_id in sector_ids:
        try:
            instruments = load_sector_instruments(sector_id)
            for inst in instruments:
                symbol_pairs.append((inst.symbol, inst.exchange, sector_id))
        except Exception as e:
            logger.warning(f"Failed to load instruments for {sector_id}: {e}")
    
    logger.info(f"Fetching news for {len(symbol_pairs)} symbols")
    
    # Parallel fetch and store
    total_fetched = 0
    total_stored = 0
    failed = []
    
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(fetch_and_store_for_symbol, cfg, sym, exch, args.refresh_snapshots)
            for sym, exch, _ in symbol_pairs
        ]
        
        for future in futures:
            try:
                result = future.result(timeout=30)
                total_fetched += result.get("fetched", 0)
                total_stored += result.get("stored", 0)
                
                if result.get("error"):
                    failed.append(result["symbol"])
                    logger.warning(f"{result['symbol']}: {result['error']}")
                else:
                    logger.debug(
                        f"{result['symbol']}: fetched={result['fetched']} "
                        f"stored={result['stored']} dups={result['duplicates']}"
                    )
            except Exception as e:
                logger.error(f"Future failed: {e}")
    
    # Summary
    logger.info("=" * 60)
    logger.info(f"BATCH COMPLETE")
    logger.info(f"  Symbols processed: {len(symbol_pairs)}")
    logger.info(f"  Total items fetched: {total_fetched}")
    logger.info(f"  Total items stored: {total_stored}")
    logger.info(f"  Failed symbols: {len(failed)}")
    if failed:
        logger.info(f"  Failed list: {', '.join(failed[:10])}")
    logger.info("=" * 60)
    
    return 0 if len(failed) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
Helper Script: scripts/cleanup_news.py
Python
#!/usr/bin/env python3
"""
Cleanup old news items from Supabase.

Usage:
  python scripts/cleanup_news.py --older-than-hours 72
"""

from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config_loader import load_config
from news_store import cleanup_old_news

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Cleanup old news")
    parser.add_argument(
        "--older-than-hours",
        type=int,
        default=72,
        help="Delete news older than this many hours (default: 72)"
    )
    args = parser.parse_args()
    
    cfg = load_config()
    
    logger.info(f"Cleaning up news older than {args.older_than_hours} hours...")
    result = cleanup_old_news(cfg, older_than_hours=args.older_than_hours)
    
    logger.info(f"Deleted {result.get('deleted', 0)} old news items")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
Testing Strategy
Unit Tests: tests/test_news_client.py
Python
"""Test news fetching and normalization."""
import pytest
import os
from datetime import datetime
from src.news_client import (
    normalize_news_item,
    fetch_all_news_for_symbol,
    deduplicate_news_items,
)


def test_normalize_news_item_newsapi():
    """Test normalization of NewsAPI format."""
    raw = {
        "title": "RELIANCE Q4 profit jumps 25%",
        "url": "https://example.com/news/123",
        "source": {"id": "moneycontrol", "name": "Moneycontrol"},
        "publishedAt": "2026-05-30T10:00:00Z",
        "description": "Strong earnings beat expectations..."
    }
    normalized = normalize_news_item(raw, "RELIANCE", "NSE", "newsapi")
    
    assert normalized["symbol"] == "RELIANCE"
    assert normalized["exchange"] == "NSE"
    assert normalized["title"] == raw["title"]
    assert normalized["url"] == raw["url"]
    assert normalized["source"] == "newsapi"
    assert normalized["relevance_score"] > 0.8


def test_deduplicate_news_items():
    """Test deduplication by URL and title hash."""
    items = [
        {
            "title": "INFY gains momentum",
            "url": "https://example.com/1",
            "symbol": "INFY",
        },
        {
            "title": "INFY gains momentum",  # Duplicate title
            "url": "https://example.com/2",
            "symbol": "INFY",
        },
        {
            "title": "TCS Q4 results",
            "url": "https://example.com/3",
            "symbol": "TCS",
        },
    ]
    deduped = deduplicate_news_items(items)
    
    assert len(deduped) == 2  # INFY duplicate removed
    urls = [item["url"] for item in deduped]
    assert "https://example.com/1" in urls
    assert "https://example.com/3" in urls


@pytest.mark.skipif(
    not os.environ.get("NEWSAPI_API_KEY"),
    reason="NewsAPI key not configured"
)
def test_fetch_news_live_newsapi():
    """Live test against NewsAPI (requires API key)."""
    items = fetch_all_news_for_symbol("INFY", "NSE")
    
    assert len(items) > 0
    assert all("symbol" in item and "title" in item for item in items)
    assert all(item["symbol"] == "INFY" for item in items)
Unit Tests: tests/test_news_sentiment.py
Python
"""Test sentiment analysis."""
import pytest
from src.news_sentiment import (
    compute_sentiment_vader,
    aggregate_sentiment,
)


def test_sentiment_vader_positive():
    """VADER should detect positive sentiment."""
    result = compute_sentiment_vader(
        "RELIANCE profit jumps 40% YoY beating all estimates"
    )
    
    assert result["sentiment"] == "positive"
    assert result["score"] > 0.3


def test_sentiment_vader_negative():
    """VADER should detect negative sentiment."""
    result = compute_sentiment_vader(
        "Stock crashes after regulatory action and compliance violation"
    )
    
    assert result["sentiment"] == "negative"
    assert result["score"] < -0.3


def test_sentiment_vader_neutral():
    """VADER should detect neutral sentiment."""
    result = compute_sentiment_vader(
        "INFY reported Q4 results with mixed performance"
    )
    
    assert result["sentiment"] in ["neutral", "mixed"]
    assert abs(result["score"]) < 0.2


def test_aggregate_sentiment_weighted():
    """Weighted aggregation should favor higher relevance items."""
    items = [
        {
            "sentiment": "positive",
            "sentiment_score": 0.8,
            "relevance_score": 0.9,
        },
        {
            "sentiment": "negative",
            "sentiment_score": -0.5,
            "relevance_score": 0.3,
        },
    ]
    agg = aggregate_sentiment(items, weight_by_relevance=True)
    
    assert agg["aggregate_sentiment"] == "positive"
    assert agg["aggregate_score"] > 0.3


def test_aggregate_sentiment_equal_weight():
    """Unweighted aggregation should average equally."""
    items = [
        {"sentiment": "positive", "sentiment_score": 0.8, "relevance_score": 0.5},
        {"sentiment": "negative", "sentiment_score": -0.8, "relevance_score": 0.5},
    ]
    agg = aggregate_sentiment(items, weight_by_relevance=False)
    
    assert agg["aggregate_sentiment"] == "neutral"
    assert abs(agg["aggregate_score"]) < 0.1
Integration Tests: tests/test_sector_audit_with_news.py
Python
"""Test news integration with sector audit."""
import os
import pytest
from datetime import datetime
from src.config_loader import load_config
from src.news_store import store_news_items, get_recent_news_for_symbol
from src.news_sentiment import aggregate_sentiment


@pytest.mark.skipif(
    not os.environ.get("SUPABASE_URL"),
    reason="Supabase not configured"
)
def test_store_and_retrieve_news():
    """Store news items and retrieve them."""
    cfg = load_config()
    
    test_items = [
        {
            "symbol": "TESTSTOCK",
            "exchange": "NSE",
            "title": "Test Company earnings beat",
            "url": "https://test.example.com/news/1",
            "source": "test",
            "published_at": datetime.now().isoformat(),
            "sentiment": "positive",
            "sentiment_score": 0.8,
            "relevance_score": 0.9,
        }
    ]
    
    # Store
    result = store_news_items(cfg, test_items)
    assert result["inserted"] >= 0
    
    # Retrieve
    retrieved = get_recent_news_for_symbol(cfg, "TESTSTOCK", "NSE", lookback_hours=1)
    assert len(retrieved) > 0
    assert any(item["title"] == test_items[0]["title"] for item in retrieved)


@pytest.mark.skipif(
    not os.environ.get("SUPABASE_URL"),
    reason="Supabase not configured"
)
def test_aggregate_sentiment_from_db():
    """Aggregate sentiment from database-sourced news."""
    cfg = load_config()
    
    # Fetch some recent news
    news = get_recent_news_for_symbol(cfg, "INFY", "NSE", limit=10)
    
    if news:
        agg = aggregate_sentiment(news)
        
        assert "aggregate_sentiment" in agg
        assert "aggregate_score" in agg
        assert isinstance(agg["aggregate_score"], (int, float))
        assert -1.0 <= agg["aggregate_score"] <= 1.0
Deployment Checklist
Pre-Deployment:

 All Python modules created and tested locally
 Supabase tables created via SQL migration
 Environment variables configured in GitHub Actions secrets
 Dependencies added to requirements.txt
 .env.example updated with news variables
 Unit tests passing locally
 Integration tests passing (if Supabase configured)
Deployment Steps:

 Create Supabase tables (run SQL schema)
 Update requirements.txt with news dependencies
 Add .py modules to src/: news_client.py, news_sentiment.py, news_store.py, news_audit.py
 Add helper scripts to scripts/: fetch_news_batch.py, cleanup_news.py
 Create GitHub Actions workflow: .github/workflows/news_fetch.yml
 Modify sector_audit.py to include news enrichment hook
 Modify brain.py system prompt
 Add tests to tests/
 Update .env.example and documentation
 Test in GitHub Actions (dry-run with limited symbols)
 Monitor logs for errors, adjust TITAN_NEWS_BLEND_WEIGHT based on live performance
 Enable news fetcher on production schedule
Post-Deployment Monitoring:

 Check GitHub Actions workflow logs for news_fetch.yml
 Verify Supabase tables are populating
 Monitor sector audit enrichment (check audit dicts for news fields)
 Verify Gemini narratives mention news drivers
 Check sentiment accuracy against manual samples
 Adjust TITAN_NEWS_BLEND_WEIGHT if priority ranking changes too much
Appendices
Appendix A: Environment Variables Reference
bash
# ===== NEWS API KEYS =====
NEWSAPI_API_KEY=                    # From https://newsapi.org
FINNHUB_API_KEY=                    # From https://finnhub.io

# ===== NEWS SOURCES =====
TITAN_NEWS_FEEDS=                   # Comma-separated RSS URLs

# ===== FRESHNESS & CACHING =====
TITAN_NEWS_MAX_AGE_HOURS=36         # How old news can be before filtering
TITAN_NEWS_SNAPSHOT_TTL_HOURS=2     # Cache validity for snapshot table
TITAN_NEWS_SNAPSHOT_TABLE=global_news_snapshots

# ===== LIMITS =====
TITAN_NEWS_FETCH_LIMIT=40           # Max items per symbol
TITAN_NEWS_DRIVER_LIMIT=3           # Top drivers included in narratives

# ===== BLENDING (for priority ranking, optional) =====
TITAN_NEWS_BLEND_WEIGHT=3.5         # Multiplier for news_sentiment_score
TITAN_NEWS_BLEND_CAP=3              # Max absolute contribution to score

# ===== SENTIMENT MODEL =====
TITAN_SENTIMENT_MODEL=vader         # 'vader' (default, fast) | 'finbert' (accurate)
Appendix B: Sample Enriched Audit Payload
JSON
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "sector": "energy",
  "benchmark": "equity",
  "z_score": 1.2,
  "intent_score": 65.0,
  "effective_intent_score": 65.0,
  "return_1d_pct": 2.1,
  "volume_participation_ratio": 1.3,
  
  "recent_news": [
    {
      "id": 12345,
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "title": "RELIANCE Q4 profit up 25% YoY, beats estimates",
      "url": "https://moneycontrol.com/news/business/reliance-q4-...",
      "source": "moneycontrol",
      "published_at": "2026-05-30T08:30:00Z",
      "sentiment": "positive",
      "sentiment_score": 0.85,
      "relevance_score": 0.95,
      "impact_level": "high",
      "event_type": "earnings"
    },
    {
      "title": "Reliance explores renewable energy expansion",
      "url": "https://...",
      "source": "reuters",
      "published_at": "2026-05-30T07:15:00Z",
      "sentiment": "neutral",
      "sentiment_score": 0.0,
      "relevance_score": 0.7,
      "impact_level": "medium"
    }
  ],
  
  "news_sentiment_aggregate": "positive",
  "news_sentiment_score": 0.72,
  "news_sentiment_trend": "strengthening",
  "news_sentiment_trend_score": 0.15,
  "news_count": 3,
  "news_price_alignment": true,
  
  "next_week_score": 68.0,
  "next_day_score": 62.0,
  
  "rows": 120,
  "option_chain_unavailable": false
}
Appendix C: Error Handling & Recovery
Issue	Root Cause	Solution
NewsAPI 401 error	Invalid API key	Verify NEWSAPI_API_KEY in GitHub secrets
Finnhub 429 (rate limit)	Too many requests	Use backoff; increase interval between fetches
Duplicate URL constraint violation	Item already in DB	Normal; query handles on_conflict via upsert
Sentiment computation timeout	VADER/FinBERT model slow	Reduce batch size; use VADER (faster than FinBERT)
Missing news in audit	Fetcher didn't run or query failed	Check news_fetch.yml workflow logs
Blank news fields in Gemini output	Sentiment not computed	Ensure sentiment computation doesn't fail silently
Contradiction flags never trigger	Price moves align by chance	Check news_price_contradiction_reason logic
Appendix D: Performance Tuning
Batch Size & Parallelism:

news_fetch.yml: Default 4 workers; increase to 8 for faster fetching (higher API rate)
fetch_news_batch.py --workers 8: Parallel symbol processing
Caching:

TITAN_NEWS_SNAPSHOT_TTL_HOURS=2: Cache freshness; increase to 4-6 for less recomputation
global_news_snapshots table: Pre-compute aggregates to avoid on-the-fly computation
Sentiment Model:

VADER (default): ~1ms per item, 100% accuracy on financial terms
FinBERT: ~0.5s per item on GPU, ~5s on CPU; ~2% higher accuracy
API Limits:

NewsAPI: 100 articles/day free tier; 500/day with key
Finnhub: 60 API calls/min free tier
RSS: No limit; self-hosted, fast
Appendix E: Extending with Custom News Sources
Adding a new news source (e.g., Twitter API):

Create a function in news_client.py:

Python
def fetch_news_from_twitter(symbol: str, ...) -> list[dict]:
    # Twitter/X API integration
    # Return normalized items
    pass
Call it from fetch_all_news_for_symbol():

Python
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {
        # ... existing sources ...
        pool.submit(fetch_news_from_twitter, symbol): "twitter",
    }
Add credentials to GitHub Actions & .env.example

Appendix F: Data Privacy & Rate Limiting
Compliance:

RSS feeds: Verify terms-of-service allow caching
NewsAPI/Finnhub: Follow API ToS; store only metadata, not full articles
GDPR/Privacy: Don't store user IP; only news metadata
Rate Limiting:

news_fetch.yml: Runs every 2 hours → ~12 fetches/day
Backoff: Catch 429 errors, increase retry delay
API key rotation: Use multiple keys if quota is shared
