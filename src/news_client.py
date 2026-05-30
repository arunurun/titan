"""News fetcher and normalization for per-symbol financial news."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import feedparser

from config_loader import TitanConfig
from news_config import get_news_api_keys, get_titan_news_feeds

logger = logging.getLogger(__name__)

_NEWS_FETCH_LIMIT_DEFAULT = 40
_NEWS_MAX_AGE_HOURS_DEFAULT = 36.0
_DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.livemint.com/rss/markets",
)
_POSITIVE_TERMS = frozenset({"surge", "beat", "growth", "wins", "approval", "record", "upgrade", "profit"})
_NEGATIVE_TERMS = frozenset({"fall", "drop", "cuts", "downgrade", "probe", "ban", "loss", "miss", "decline"})


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _news_fetch_limit() -> int:
    raw = (str(os.environ.get("TITAN_NEWS_FETCH_LIMIT", "")) or "").strip()
    if not raw:
        return _NEWS_FETCH_LIMIT_DEFAULT
    try:
        return max(5, int(raw))
    except ValueError:
        return _NEWS_FETCH_LIMIT_DEFAULT


def _news_max_age_hours() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", "")) or "").strip()
    if not raw:
        return _NEWS_MAX_AGE_HOURS_DEFAULT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _NEWS_MAX_AGE_HOURS_DEFAULT


def _configured_rss_feeds(cfg: TitanConfig | None = None) -> list[str]:
    raw = (get_titan_news_feeds(cfg) or str(os.environ.get("TITAN_NEWS_FEEDS", "")) or "").strip()
    if not raw:
        return list(_DEFAULT_RSS_FEEDS)
    vals = [x.strip() for x in raw.split(",")]
    return [x for x in vals if x]


def _normalize_text(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _title_hash(title: str) -> str:
    return hashlib.sha256(_normalize_text(title).encode("utf-8")).hexdigest()


def _parse_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc) if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = parsedate_to_datetime(txt)
        if dt is not None:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rss_source_label(feed_url: str, entry: Any) -> str:
    source = getattr(entry, "source", None)
    if source is not None:
        title = getattr(source, "title", None) or getattr(source, "id", None)
        if title:
            return str(title).strip()
    host = urlparse(feed_url).netloc.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "rss"


def _compute_relevance_score(*, symbol: str, title: str, summary: str = "") -> float:
    sym = str(symbol or "").strip().upper()
    text = _normalize_text(f"{title} {summary}")
    if not text:
        return 0.0
    score = 0.35
    if sym and re.search(rf"\b{re.escape(sym.lower())}\b", text):
        score += 0.45
    elif sym and sym.lower() in text:
        score += 0.25
    pos_hits = sum(1 for term in _POSITIVE_TERMS if term in text)
    neg_hits = sum(1 for term in _NEGATIVE_TERMS if term in text)
    if pos_hits or neg_hits:
        score += min(0.15, (pos_hits + neg_hits) * 0.05)
    return round(_clamp(score, 0.0, 1.0), 4)


def _impact_level_from_text(title: str, summary: str = "") -> str:
    text = _normalize_text(f"{title} {summary}")
    high_terms = ("earnings", "results", "regulatory", "sebi", "rbi", "merger", "acquisition", "fda")
    low_terms = ("preview", "commentary", "opinion", "podcast")
    if any(term in text for term in high_terms):
        return "high"
    if any(term in text for term in low_terms):
        return "low"
    return "medium"


def _finnhub_symbol(symbol: str, exchange: str = "NSE") -> str:
    sym = str(symbol or "").strip().upper()
    ex = str(exchange or "NSE").strip().upper()
    if not sym:
        return sym
    if "." in sym:
        return sym
    if ex == "NSE":
        return f"{sym}.NS"
    if ex == "BSE":
        return f"{sym}.BO"
    return sym


def normalize_news_item(
    raw: dict[str, Any],
    symbol: str,
    exchange: str,
    source: str,
) -> dict[str, Any]:
    """Convert source-specific payload to standardized news dict."""
    title = str(raw.get("title") or raw.get("headline") or "").strip()
    url = str(raw.get("url") or raw.get("link") or "").strip()
    summary = str(raw.get("summary") or raw.get("description") or raw.get("content") or "").strip()
    published_raw = (
        raw.get("published_at")
        or raw.get("publishedAt")
        or raw.get("datetime")
        or raw.get("time")
        or raw.get("pubDate")
        or ""
    )
    published_at = _parse_timestamp(published_raw)
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    src = str(source or raw.get("source") or "unknown").strip()
    if isinstance(raw.get("source"), dict):
        nested = raw["source"]
        src = str(nested.get("name") or nested.get("id") or src).strip()
    relevance = raw.get("relevance_score")
    if relevance is None:
        relevance = _compute_relevance_score(symbol=symbol, title=title, summary=summary)
    else:
        relevance = round(_clamp(float(relevance), 0.0, 1.0), 4)
    return {
        "symbol": str(symbol or "").strip().upper(),
        "exchange": str(exchange or "NSE").strip().upper(),
        "title": title,
        "url": url,
        "source": src,
        "published_at": published_at.isoformat(),
        "summary": summary,
        "relevance_score": relevance,
        "impact_level": str(raw.get("impact_level") or _impact_level_from_text(title, summary)),
        "event_type": str(raw.get("event_type") or "general"),
        "sentiment": str(raw.get("sentiment") or "neutral"),
        "sentiment_score": float(raw.get("sentiment_score") or 0.0),
        "sentiment_model": str(raw.get("sentiment_model") or "vader"),
        "is_duplicate": bool(raw.get("is_duplicate") or False),
    }


def deduplicate_news_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicates by URL (primary) and title hash (secondary)."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip().lower()
        title_key = _title_hash(str(item.get("title") or ""))
        if url and url in seen_urls:
            continue
        if title_key in seen_titles:
            continue
        if url:
            seen_urls.add(url)
        seen_titles.add(title_key)
        out.append(item)
    return out


def fetch_news_from_newsapi(
    symbol: str,
    exchange: str = "NSE",
    lookback_hours: int = 24,
    api_key: str | None = None,
    cfg: TitanConfig | None = None,
) -> list[dict[str, Any]]:
    """Fetch from NewsAPI (free tier: up to 100 articles/day)."""
    key = (api_key or get_news_api_keys(cfg).newsapi_api_key or "").strip()
    if not key:
        logger.info("NewsAPI skipped for %s: missing NEWSAPI_API_KEY", symbol)
        return []
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    try:
        from newsapi import NewsApiClient
    except ImportError:
        logger.warning("newsapi-python not installed; NewsAPI fetch skipped")
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    query = f"{sym} OR {sym} stock India"
    try:
        client = NewsApiClient(api_key=key)
        payload = client.get_everything(
            q=query,
            language="en",
            sort_by="publishedAt",
            page_size=min(100, _news_fetch_limit()),
        )
    except Exception as exc:
        logger.warning("NewsAPI fetch failed for %s: %s", sym, exc)
        return []
    articles = payload.get("articles") if isinstance(payload, dict) else []
    if not isinstance(articles, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in articles:
        if not isinstance(raw, dict):
            continue
        published_at = _parse_timestamp(raw.get("publishedAt"))
        if published_at is not None and published_at < cutoff:
            continue
        normalized = normalize_news_item(raw, sym, exchange, "newsapi")
        if normalized.get("title") and normalized.get("url"):
            out.append(normalized)
    return out


def fetch_news_from_finnhub(
    symbol: str,
    api_key: str | None = None,
    lookback_hours: int = 24,
    *,
    exchange: str = "NSE",
    cfg: TitanConfig | None = None,
) -> list[dict[str, Any]]:
    """Fetch from Finnhub (Indian market coverage, real-time)."""
    key = (api_key or get_news_api_keys(cfg).finnhub_api_key or "").strip()
    if not key:
        logger.info("Finnhub skipped for %s: missing FINNHUB_API_KEY", symbol)
        return []
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    try:
        import finnhub
    except ImportError:
        logger.warning("finnhub-python not installed; Finnhub fetch skipped")
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    fh_symbol = _finnhub_symbol(sym, exchange)
    client = finnhub.Client(api_key=key)
    raw_items: list[dict[str, Any]] = []
    for attempt in range(3):
        try:
            company_news = client.company_news(fh_symbol, _from_date(cutoff), _to_date(datetime.now(timezone.utc)))
            if isinstance(company_news, list):
                raw_items = [x for x in company_news if isinstance(x, dict)]
            break
        except Exception as exc:
            err = str(exc).lower()
            if "429" in err or "rate" in err:
                delay = 2 ** attempt
                logger.info("Finnhub rate limit for %s; backoff %ss", sym, delay)
                time_module.sleep(delay)
                continue
            logger.warning("Finnhub fetch failed for %s: %s", sym, exc)
            return []
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        ts = _parse_timestamp(raw.get("datetime"))
        if ts is not None and ts < cutoff:
            continue
        mapped = {
            "title": raw.get("headline") or raw.get("title"),
            "url": raw.get("url") or raw.get("link"),
            "summary": raw.get("summary") or raw.get("description"),
            "published_at": ts.isoformat() if ts else None,
            "source": raw.get("source") or "finnhub",
        }
        normalized = normalize_news_item(mapped, sym, exchange, "finnhub")
        if normalized.get("title") and normalized.get("url"):
            out.append(normalized)
    return out


def _from_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _to_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_feed_entry_time(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(time_module.mktime(parsed), tz=timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return _parse_timestamp(getattr(entry, "published", "") or getattr(entry, "updated", ""))


def fetch_news_from_rss_feeds(
    feeds: list[str] | None = None,
    symbol: str = "",
    lookback_hours: int = 24,
    *,
    exchange: str = "NSE",
    cfg: TitanConfig | None = None,
) -> list[dict[str, Any]]:
    """Parse RSS/Atom feeds and filter by symbol relevance."""
    feed_urls = feeds if feeds else _configured_rss_feeds(cfg)
    sym = str(symbol or "").strip().upper()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    out: list[dict[str, Any]] = []
    for feed_url in feed_urls:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.info("RSS feed skipped url=%s reason=%s", feed_url, exc)
            continue
        entries = getattr(parsed, "entries", None) or []
        for entry in entries:
            title = str(getattr(entry, "title", "") or "").strip()
            link = str(getattr(entry, "link", "") or "").strip()
            summary = str(getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
            ts = _parse_feed_entry_time(entry)
            if ts is not None and ts < cutoff:
                continue
            if sym:
                relevance = _compute_relevance_score(symbol=sym, title=title, summary=summary)
                if relevance < 0.35:
                    continue
            else:
                relevance = 0.5
            source_label = _rss_source_label(feed_url, entry)
            raw = {
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": ts.isoformat() if ts else None,
                "source": f"rss:{source_label}",
                "relevance_score": relevance,
            }
            if not title:
                continue
            normalized = normalize_news_item(raw, sym or "MACRO", exchange, raw["source"])
            out.append(normalized)
    return out


def _rank_news_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[float, float]:
        rel = float(item.get("relevance_score") or 0.0)
        ts = _parse_timestamp(item.get("published_at"))
        ts_val = ts.timestamp() if ts is not None else 0.0
        return (rel, ts_val)

    return sorted(items, key=sort_key, reverse=True)


def fetch_all_news_for_symbol(
    symbol: str,
    exchange: str = "NSE",
    cfg: TitanConfig | Any | None = None,
) -> list[dict[str, Any]]:
    """Orchestrate all sources, deduplicate, and rank by relevance."""
    titan_cfg = cfg if isinstance(cfg, TitanConfig) else None
    api_keys = get_news_api_keys(titan_cfg)
    sym = str(symbol or "").strip().upper()
    ex = str(exchange or "NSE").strip().upper()
    if not sym:
        return []
    lookback = int(_news_max_age_hours())
    max_items = _news_fetch_limit()
    stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    combined: list[dict[str, Any]] = []

    def _collect(source_fn: Any, label: str) -> list[dict[str, Any]]:
        try:
            if label == "rss":
                return source_fn(
                    feeds=None,
                    symbol=sym,
                    lookback_hours=lookback,
                    exchange=ex,
                    cfg=titan_cfg,
                )
            if label == "finnhub":
                return source_fn(
                    sym,
                    lookback_hours=lookback,
                    exchange=ex,
                    api_key=api_keys.finnhub_api_key,
                    cfg=titan_cfg,
                )
            if label == "newsapi":
                return source_fn(
                    sym,
                    exchange=ex,
                    lookback_hours=lookback,
                    api_key=api_keys.newsapi_api_key,
                    cfg=titan_cfg,
                )
            return source_fn(sym, exchange=ex, lookback_hours=lookback)
        except Exception as exc:
            logger.warning("News source %s failed for %s: %s", label, sym, exc)
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_collect, fetch_news_from_newsapi, "newsapi"): "newsapi",
            pool.submit(_collect, fetch_news_from_finnhub, "finnhub"): "finnhub",
            pool.submit(_collect, fetch_news_from_rss_feeds, "rss"): "rss",
        }
        for future in as_completed(futures):
            try:
                combined.extend(future.result())
            except Exception as exc:
                logger.warning("News fetch future failed for %s: %s", sym, exc)

    fresh: list[dict[str, Any]] = []
    for item in combined:
        ts = _parse_timestamp(item.get("published_at"))
        if ts is not None and ts < stale_cutoff:
            continue
        fresh.append(item)
    deduped = deduplicate_news_items(fresh)
    ranked = _rank_news_items(deduped)
    return ranked[:max_items]
