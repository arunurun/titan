"""Sector priority ranking utilities (NSE market-cap enriched, Supabase persisted)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import pandas as pd
from postgrest.exceptions import APIError
from supabase import create_client

from breeze_client import fetch_equity_data, volume_participation_ratio
from config_loader import TitanConfig
from sector_registry import SectorInstrument

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_NSE_HOME_URL = "https://www.nseindia.com"
_NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
_YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
_MONEYCONTROL_SUGGEST_URL = (
    "https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php?query={query}&type=1&format=json"
)
_SCREENER_SEARCH_URL = "https://www.screener.in/api/company/search/?q={query}"
_DEFAULT_NEWS_FEEDS: tuple[str, ...] = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
)
_STOCK_NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
_NEWS_STALE_HOURS_DEFAULT = 36.0
_NEWS_SNAPSHOT_TTL_HOURS_DEFAULT = 2.0
_NEWS_FETCH_LIMIT_DEFAULT = 40
_STOCK_NEWS_FETCH_LIMIT_DEFAULT = 8
_NEWS_BLEND_WEIGHT_DEFAULT = 3.5
_NEWS_BLEND_CAP_DEFAULT = 3.0
_NEWS_DRIVER_LIMIT_DEFAULT = 3
_NEWS_SNAPSHOT_TABLE_DEFAULT = "global_news_snapshots"
_SECTOR_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai": (
        "artificial intelligence",
        "ai chip",
        "gpu",
        "llm",
        "machine learning",
        "model training",
        "semiconductor",
    ),
    "defence": (
        "defence",
        "defense",
        "military",
        "missile",
        "aerospace",
        "warship",
        "procurement",
        "border security",
    ),
    "data_centre": (
        "data centre",
        "data center",
        "datacenter",
        "cloud region",
        "colocation",
        "server farm",
        "hyperscale",
    ),
    "electronics_ems": (
        "electronics manufacturing",
        "ems",
        "contract manufacturing",
        "pcb",
        "assembly plant",
    ),
    "renewables_clean_energy": (
        "solar",
        "wind power",
        "green hydrogen",
        "renewable energy",
        "battery storage",
    ),
    "railways_transport_infra": (
        "railway",
        "rolling stock",
        "metro rail",
        "freight corridor",
        "transport infrastructure",
    ),
}
_POSITIVE_NEWS_TERMS = {
    "surge",
    "expand",
    "growth",
    "wins",
    "approval",
    "record",
    "upgrade",
    "investment",
    "funding",
    "boost",
}
_NEGATIVE_NEWS_TERMS = {
    "fall",
    "drop",
    "cuts",
    "downgrade",
    "probe",
    "ban",
    "risk",
    "lawsuit",
    "crisis",
    "shortage",
}
_IMPACT_NEWS_TERMS = {
    "tariff": 0.35,
    "sanction": 0.45,
    "policy": 0.25,
    "regulation": 0.3,
    "budget": 0.3,
    "rate hike": 0.35,
    "merger": 0.25,
    "acquisition": 0.25,
    "contract": 0.2,
    "capex": 0.3,
    "bulk deal": 0.4,
    "block deal": 0.4,
    "large deal": 0.35,
    "fii": 0.3,
    "dii": 0.3,
    "fpi": 0.28,
    "stake": 0.3,
    "shareholding": 0.25,
    "insider": 0.25,
    "promoter": 0.25,
    "qip": 0.3,
    "preferential": 0.28,
    "allotment": 0.22,
    "order win": 0.25,
}
_STOCK_NEWS_NEGATIVE_PATTERNS_DEFAULT: tuple[str, ...] = (
    "stocks to buy",
    "stock picks",
    "top stocks",
    "multibagger",
    "buy call",
    "sell call",
    "target price",
    "price target",
    "buy target",
    "recommend",
    "recommendation",
    "rating",
    "upgrade to buy",
    "downgrade to sell",
    "motilal oswal",
    "icici direct",
    "sharekhan",
    "angel one",
    "compared to",
    "investment guru",
    "yahoo finance recommends",
    "msn money",
)
_STOCK_NEWS_POSITIVE_SIGNALS_DEFAULT: tuple[str, ...] = (
    "bulk deal",
    "block deal",
    "large deal",
    "insider",
    "promoter",
    "stake",
    "acquisition",
    "takeover",
    "fii",
    "dii",
    "fpi",
    "pms",
    "aif",
    "mutual fund",
    "shareholding",
    "allotment",
    "qip",
    "preferential",
    "board meeting",
    "results",
    "order win",
    "capex",
    "plant",
    "capacity expansion",
    "iceberg",
)
_STOCK_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "india",
        "indian",
        "limited",
        "ltd",
        "company",
        "corp",
        "corporation",
        "motor",
        "motors",
        "industries",
        "industry",
        "holdings",
        "enterprises",
        "the",
        "and",
    }
)
_STOCK_NEWS_AGGREGATOR_FRAGMENTS: tuple[str, ...] = (
    "google news",
    "news.google",
    "msn",
    "yahoo",
    "investment guru",
)
_STOCK_NEWS_QUALITY_SOURCE_FRAGMENTS_DEFAULT: tuple[str, ...] = (
    "livemint.com",
    "economictimes.indiatimes.com",
    "moneycontrol.com",
    "business-standard.com",
    "financialexpress.com",
)
_STOCK_NEWS_NSE_SOURCES: frozenset[str] = frozenset(
    {
        "nse_bulk_deals",
        "nse_block_deals",
        "nse_corporate_announcements",
    }
)
_STOCK_NEWS_MIN_RELEVANCE_DEFAULT = 0.35
_STOCK_NEWS_RECO_CONTEXT_TERMS: frozenset[str] = frozenset(
    {
        "target",
        "recommend",
        "rating",
        "buy",
        "sell",
        "price",
        "broker",
        "analyst",
    }
)
_NSE_BULK_BLOCK_URL = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
_NSE_CORPORATE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _round_or_none(x: float, digits: int = 4) -> float | None:
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


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


def _stock_news_fetch_limit() -> int:
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_FETCH_LIMIT", "")) or "").strip()
    if not raw:
        return _STOCK_NEWS_FETCH_LIMIT_DEFAULT
    try:
        return max(2, int(raw))
    except ValueError:
        return _STOCK_NEWS_FETCH_LIMIT_DEFAULT


def _news_max_age_hours() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", "")) or "").strip()
    if not raw:
        return _NEWS_STALE_HOURS_DEFAULT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _NEWS_STALE_HOURS_DEFAULT


def _news_snapshot_ttl_hours() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_SNAPSHOT_TTL_HOURS", "")) or "").strip()
    if not raw:
        return _NEWS_SNAPSHOT_TTL_HOURS_DEFAULT
    try:
        return max(0.25, float(raw))
    except ValueError:
        return _NEWS_SNAPSHOT_TTL_HOURS_DEFAULT


def _news_snapshot_table_name() -> str:
    raw = (str(os.environ.get("TITAN_NEWS_SNAPSHOT_TABLE", "")) or "").strip()
    if not raw:
        return _NEWS_SNAPSHOT_TABLE_DEFAULT
    return raw


def _news_blend_weight() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_BLEND_WEIGHT", "")) or "").strip()
    if not raw:
        return _NEWS_BLEND_WEIGHT_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _NEWS_BLEND_WEIGHT_DEFAULT


def _news_blend_cap() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_BLEND_CAP", "")) or "").strip()
    if not raw:
        return _NEWS_BLEND_CAP_DEFAULT
    try:
        return max(0.5, float(raw))
    except ValueError:
        return _NEWS_BLEND_CAP_DEFAULT


def _news_driver_limit() -> int:
    raw = (str(os.environ.get("TITAN_NEWS_DRIVER_LIMIT", "")) or "").strip()
    if not raw:
        return _NEWS_DRIVER_LIMIT_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _NEWS_DRIVER_LIMIT_DEFAULT


def _configured_news_feeds() -> list[str]:
    raw = (str(os.environ.get("TITAN_NEWS_FEEDS", "")) or "").strip()
    if not raw:
        return list(_DEFAULT_NEWS_FEEDS)
    vals = [x.strip() for x in raw.split(",")]
    return [x for x in vals if x]


def _env_csv_terms(env_key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = (str(os.environ.get(env_key, "")) or "").strip()
    if not raw:
        return default
    vals = tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    return vals or default


def _stock_news_negative_patterns() -> tuple[str, ...]:
    return _env_csv_terms("TITAN_STOCK_NEWS_NEGATIVE_KEYWORDS", _STOCK_NEWS_NEGATIVE_PATTERNS_DEFAULT)


def _stock_news_positive_signals() -> tuple[str, ...]:
    return _env_csv_terms("TITAN_STOCK_NEWS_POSITIVE_KEYWORDS", _STOCK_NEWS_POSITIVE_SIGNALS_DEFAULT)


def _stock_news_quality_source_fragments() -> tuple[str, ...]:
    return _env_csv_terms(
        "TITAN_STOCK_NEWS_QUALITY_SOURCES",
        _STOCK_NEWS_QUALITY_SOURCE_FRAGMENTS_DEFAULT,
    )


def _stock_news_min_relevance() -> float:
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_MIN_RELEVANCE", "")) or "").strip()
    if not raw:
        return _STOCK_NEWS_MIN_RELEVANCE_DEFAULT
    try:
        return _clamp(float(raw), 0.05, 0.95)
    except ValueError:
        return _STOCK_NEWS_MIN_RELEVANCE_DEFAULT


def _stock_news_enable_nse() -> bool:
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_ENABLE_NSE", "")) or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _normalize_news_text(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _parse_news_timestamp(raw: str) -> datetime | None:
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


def _news_source_label(feed_url: str, channel_title: str | None) -> str:
    c = str(channel_title or "").strip()
    if c:
        return c
    host = urlparse(feed_url).netloc.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "unknown_source"


def _parse_rss_feed_items(feed_url: str, raw: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    channel = root.find("./channel")
    channel_title = channel.findtext("title") if channel is not None else None
    source = _news_source_label(feed_url, channel_title)
    out: list[dict[str, Any]] = []
    rss_items = root.findall("./channel/item")
    atom_items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    if not rss_items and not atom_items:
        atom_items = root.findall("./entry")
    for item in rss_items:
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        description = str(item.findtext("description") or "").strip()
        ts_raw = (
            item.findtext("pubDate")
            or item.findtext("published")
            or item.findtext("updated")
            or ""
        )
        ts = _parse_news_timestamp(ts_raw)
        if not title or ts is None:
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "summary": description,
                "source": source,
                "published_at": ts,
            }
        )
    for item in atom_items:
        title = str(item.findtext("{http://www.w3.org/2005/Atom}title") or item.findtext("title") or "").strip()
        link = ""
        atom_link = item.find("{http://www.w3.org/2005/Atom}link")
        if atom_link is not None:
            link = str(atom_link.attrib.get("href", "")).strip()
        if not link:
            legacy_link = item.find("link")
            if legacy_link is not None:
                link = str(legacy_link.attrib.get("href") or legacy_link.text or "").strip()
        summary = str(
            item.findtext("{http://www.w3.org/2005/Atom}summary")
            or item.findtext("summary")
            or item.findtext("{http://www.w3.org/2005/Atom}content")
            or ""
        ).strip()
        ts_raw = (
            item.findtext("{http://www.w3.org/2005/Atom}updated")
            or item.findtext("{http://www.w3.org/2005/Atom}published")
            or item.findtext("updated")
            or item.findtext("published")
            or ""
        )
        ts = _parse_news_timestamp(ts_raw)
        if not title or ts is None:
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "summary": summary,
                "source": source,
                "published_at": ts,
            }
        )
    return out


def fetch_latest_global_news(*, timeout_seconds: float = 12.0, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now = now_utc or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=_news_max_age_hours())
    max_items = _news_fetch_limit()
    deduped: dict[str, dict[str, Any]] = {}
    for feed in _configured_news_feeds():
        raw, err = _http_get_text(feed, timeout_seconds=timeout_seconds)
        if raw is None:
            logger.info("Global news feed skipped url=%s reason=%s", feed, err)
            continue
        parsed = _parse_rss_feed_items(feed, raw)
        for item in parsed:
            ts = item.get("published_at")
            if not isinstance(ts, datetime):
                continue
            if ts < stale_cutoff:
                continue
            title_key = _normalize_news_text(str(item.get("title", "")))
            url_key = str(item.get("url", "")).strip().lower()
            dedupe_key = f"{title_key}|{url_key}" if url_key else title_key
            if not dedupe_key:
                continue
            existing = deduped.get(dedupe_key)
            if existing is None or ts > existing.get("published_at", datetime.min.replace(tzinfo=timezone.utc)):
                deduped[dedupe_key] = item
    ordered = sorted(
        deduped.values(),
        key=lambda x: x.get("published_at", datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for item in ordered[:max_items]:
        out.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "summary": str(item.get("summary", "")).strip(),
                "source": str(item.get("source", "unknown_source")).strip() or "unknown_source",
                "published_at": item.get("published_at").isoformat(),
            }
        )
    return out


def _instrument_alias_candidates(
    cfg: TitanConfig,
    *,
    symbol: str,
    exchange: str,
) -> list[str]:
    sym = str(symbol or "").strip().upper()
    ex = str(exchange or "").strip().upper()
    if not sym or ex not in ("NSE", "BSE"):
        return [sym] if sym else []
    out: list[str] = [sym]
    try:
        client = create_client(cfg.supabase_url, cfg.supabase_key)
        res = (
            client.table("market_instruments")
            .select("symbol,instrument_name,breeze_stock_code")
            .eq("exchange", ex)
            .eq("symbol", sym)
            .limit(1)
            .execute()
        )
    except Exception:
        return out
    rows = list(getattr(res, "data", None) or [])
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    for raw in (
        row.get("instrument_name"),
        row.get("breeze_stock_code"),
    ):
        txt = str(raw or "").strip()
        if not txt:
            continue
        if txt.upper() == sym:
            continue
        if txt not in out:
            out.append(txt)
    return out


def _stock_news_query_exclusions() -> str:
    return '-recommend -"stocks to buy" -target -"stock picks" -multibagger'


def _stock_news_name_tokens(*, symbol: str, aliases: list[str]) -> list[str]:
    sym = str(symbol or "").strip().upper()
    tokens: list[str] = []
    if sym:
        tokens.append(sym.lower())
    for raw in aliases:
        for part in re.split(r"[^a-zA-Z0-9]+", str(raw or "")):
            tok = part.strip().lower()
            if len(tok) < 4 or tok in _STOCK_NAME_STOPWORDS:
                continue
            if tok not in tokens:
                tokens.append(tok)
    return tokens


def _stock_news_negative_reason(text: str) -> str:
    t = _normalize_news_text(text)
    if not t:
        return ""
    for pattern in _stock_news_negative_patterns():
        if pattern in t:
            return f"negative:{pattern}"
    if " vs " in t or " versus " in t:
        return "negative:comparison"
    if re.search(r"\d+\s+(stocks|shares)\s+to\s+buy", t):
        return "negative:listicle"
    if re.search(r"\d+\s+(stocks|shares)\b", t) and ("buy" in t or "pick" in t):
        return "negative:listicle"
    return ""


def _stock_news_title_identity_match(*, symbol: str, aliases: list[str], title: str) -> bool:
    title_norm = _normalize_news_text(title)
    if not title_norm:
        return False
    sym = str(symbol or "").strip().upper()
    if sym and re.search(rf"\b{re.escape(sym.lower())}\b", title_norm):
        return True
    for tok in _stock_news_name_tokens(symbol=symbol, aliases=aliases):
        if re.search(rf"\b{re.escape(tok)}\b", title_norm):
            return True
    return False


def _stock_news_summary_identity_match(*, symbol: str, aliases: list[str], summary: str) -> bool:
    summary_norm = _normalize_news_text(summary)
    if not summary_norm:
        return False
    sym = str(symbol or "").strip().upper()
    if sym and re.search(rf"\b{re.escape(sym.lower())}\b", summary_norm):
        return True
    for tok in _stock_news_name_tokens(symbol=symbol, aliases=aliases):
        if re.search(rf"\b{re.escape(tok)}\b", summary_norm):
            return True
    return False


def _stock_news_relevance_score(*, symbol: str, aliases: list[str], item: dict[str, Any]) -> float:
    source = str(item.get("source") or "").strip().lower()
    if source in _STOCK_NEWS_NSE_SOURCES:
        return 1.0
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    text = _normalize_news_text(f"{title} {summary}")
    score = 0.0
    sym = str(symbol or "").strip().upper()
    title_norm = _normalize_news_text(title)
    if sym and re.search(rf"\b{re.escape(sym.lower())}\b", title_norm):
        score += 0.35
    elif _stock_news_title_identity_match(symbol=symbol, aliases=aliases, title=title):
        title_norm = _normalize_news_text(title)
        name_hits = sum(
            1
            for tok in _stock_news_name_tokens(symbol=symbol, aliases=aliases)
            if re.search(rf"\b{re.escape(tok)}\b", title_norm)
        )
        score += 0.35 if name_hits >= 2 else 0.25
    elif _stock_news_summary_identity_match(symbol=symbol, aliases=aliases, summary=summary):
        score += 0.05
        score -= 0.20
    else:
        return 0.0
    url = str(item.get("url") or "").lower()
    source_blob = f"{source} {url}"
    if any(fragment in source_blob for fragment in _stock_news_quality_source_fragments()):
        score += 0.15
    if any(fragment in source_blob for fragment in _STOCK_NEWS_AGGREGATOR_FRAGMENTS):
        score -= 0.10
    pos_hits = sum(1 for sig in _stock_news_positive_signals() if sig in text)
    score += min(0.30, pos_hits * 0.10)
    return round(_clamp(score, 0.0, 1.0), 4)


def _filter_stock_news_items(
    *,
    symbol: str,
    aliases: list[str],
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    min_relevance = _stock_news_min_relevance()
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    top_relevance = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title:
            rejected.append({"title": "", "reason": "missing_title"})
            continue
        source = str(item.get("source") or "").strip().lower()
        if source not in _STOCK_NEWS_NSE_SOURCES:
            neg_reason = _stock_news_negative_reason(f"{title} {summary}")
            if neg_reason:
                rejected.append({"title": title[:120], "reason": neg_reason})
                continue
            if not _stock_news_title_identity_match(symbol=symbol, aliases=aliases, title=title):
                if not _stock_news_summary_identity_match(symbol=symbol, aliases=aliases, summary=summary):
                    rejected.append({"title": title[:120], "reason": "identity_mismatch"})
                    continue
        relevance = _stock_news_relevance_score(symbol=symbol, aliases=aliases, item=item)
        top_relevance = max(top_relevance, relevance)
        if relevance < min_relevance:
            rejected.append({"title": title[:120], "reason": f"low_relevance:{relevance:.2f}"})
            continue
        out_item = dict(item)
        out_item["relevance_score"] = relevance
        kept.append(out_item)
    meta = {
        "filtered_count": len(rejected),
        "rejection_samples": rejected[:3],
        "relevance_top_score": round(top_relevance, 4),
        "min_relevance": min_relevance,
    }
    return kept, meta


def _stock_news_query_candidates(*, symbol: str, aliases: list[str]) -> tuple[list[str], list[str]]:
    sym = str(symbol or "").strip().upper()
    exclusions = _stock_news_query_exclusions()
    legal_name = ""
    for raw in aliases:
        q = str(raw or "").strip()
        if q and q.upper() != sym:
            legal_name = q
            break
    primary: list[str] = []
    if legal_name:
        primary.append(f'"{legal_name}" NSE when:7d {exclusions}')
        primary.append(f'"{legal_name}" when:7d bulk OR block OR stake OR results {exclusions}')
    if sym:
        primary.append(f"{sym} NSE when:7d {exclusions}")
        primary.append(f"{sym} stock when:7d {exclusions}")
    fallback: list[str] = []
    if legal_name:
        fallback.append(f'"{legal_name}" stock India')
        fallback.append(f'"{legal_name}" NSE')
    if sym:
        fallback.append(f"{sym} stock India")
        fallback.append(f"{sym} NSE")
    for raw in [sym, *aliases]:
        q = str(raw or "").strip()
        if not q:
            continue
        if q not in primary and q not in fallback:
            fallback.append(q)
    return primary, fallback


def _fetch_google_news_rss_items(
    queries: list[str],
    *,
    timeout_seconds: float,
    symbol: str = "",
) -> tuple[list[dict[str, Any]], str, str, str]:
    """Return (items, used_query, used_alias, last_error)."""
    raw_items: list[dict[str, Any]] = []
    used_query = ""
    used_alias = ""
    last_error = ""
    last_attempted_query = ""
    sym_upper = str(symbol or "").strip().upper()
    for query in queries:
        qtxt = str(query or "").strip()
        if not qtxt:
            continue
        last_attempted_query = qtxt
        q = urllib.parse.quote(qtxt)
        feed_url = _STOCK_NEWS_SEARCH_URL.format(query=q)
        raw, err = _http_get_text(feed_url, timeout_seconds=timeout_seconds)
        if raw is None:
            last_error = str(err or "request_error")
            continue
        parsed = _parse_rss_feed_items(feed_url, raw)
        if parsed:
            raw_items.extend(parsed)
            if not used_query:
                used_query = qtxt
                if sym_upper and qtxt.strip().upper() != sym_upper:
                    used_alias = qtxt
            last_error = ""
            break
        last_error = "empty_feed"
    if not used_query and last_attempted_query:
        used_query = last_attempted_query
    return raw_items, used_query, used_alias, last_error


def _dedupe_recent_news_items(
    items: list[dict[str, Any]],
    *,
    now_utc: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    stale_cutoff = now_utc - timedelta(hours=_news_max_age_hours())
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        ts = item.get("published_at")
        if not isinstance(ts, datetime):
            continue
        if ts < stale_cutoff:
            continue
        title_key = _normalize_news_text(str(item.get("title", "")))
        url_key = str(item.get("url", "")).strip().lower()
        dedupe_key = f"{title_key}|{url_key}" if url_key else title_key
        if not dedupe_key:
            continue
        existing = deduped.get(dedupe_key)
        if existing is None or ts > existing.get("published_at", datetime.min.replace(tzinfo=timezone.utc)):
            deduped[dedupe_key] = item
    ordered = sorted(
        deduped.values(),
        key=lambda x: x.get("published_at", datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for item in ordered[: max(1, int(limit))]:
        out.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "summary": str(item.get("summary", "")).strip(),
                "source": str(item.get("source", "unknown_source")).strip() or "unknown_source",
                "published_at": item.get("published_at").isoformat(),
            }
        )
    return out


def _nse_date_range(days: int, *, now_utc: datetime | None = None) -> tuple[str, str]:
    now = now_utc or datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, int(days)))
    return start.strftime("%d-%m-%Y"), now.strftime("%d-%m-%Y")


def _fetch_nse_api(
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout_seconds: float = 20.0,
) -> tuple[Any | None, str]:
    query = urllib.parse.urlencode(params or {})
    full_url = f"{url}?{query}" if query else url
    try:
        urllib.request.urlopen(
            _build_nse_payload_request(_NSE_HOME_URL),
            timeout=timeout_seconds,
        ).read()
    except Exception:
        return None, "nse_cookie_failed"
    try:
        with urllib.request.urlopen(
            _build_nse_payload_request(full_url),
            timeout=timeout_seconds,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"nse_http_{int(getattr(e, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, "nse_request_error"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "nse_invalid_json"
    return payload, ""


def _parse_nse_trade_date(raw: Any) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _parse_news_timestamp(txt)


def _normalize_nse_deal_row(row: dict[str, Any], *, deal_type: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    sym = str(row.get("BD_SYMBOL") or row.get("symbol") or "").strip().upper()
    if not sym:
        return None
    side = str(row.get("BD_BUY_SELL") or row.get("buySell") or "").strip().upper()
    qty = row.get("BD_QTY_TRD") or row.get("qty") or ""
    price = row.get("BD_TP_WATP") or row.get("watp") or ""
    client = str(row.get("BD_CLIENT_NAME") or row.get("clientName") or "").strip()
    deal_label = "Block deal" if deal_type == "block_deals" else "Bulk deal"
    title = f"{sym}: {deal_label} — {side} {qty} @ {price}".strip(" @")
    if client:
        title = f"{title} ({client})"
    ts = _parse_nse_trade_date(row.get("BD_DT_DATE") or row.get("date"))
    if ts is None:
        ts = datetime.now(timezone.utc)
    source = "nse_block_deals" if deal_type == "block_deals" else "nse_bulk_deals"
    return {
        "title": title,
        "url": f"https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
        "summary": f"NSE {deal_label.lower()} for {sym}",
        "source": source,
        "published_at": ts,
    }


def _normalize_nse_announcement_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or row.get("sm_symbol") or "").strip().upper()
    subject = str(row.get("desc") or row.get("subject") or row.get("sm_desc") or "").strip()
    attachment = str(row.get("attchmntText") or row.get("attchmnttext") or "").strip()
    if not sym and not subject:
        return None
    title = f"{sym}: {subject}".strip(": ") if sym else subject
    if not title:
        return None
    ts = _parse_nse_trade_date(row.get("an_dt") or row.get("sort_date") or row.get("exchdisstime"))
    if ts is None:
        ts = datetime.now(timezone.utc)
    url = str(row.get("attchmntFile") or row.get("attchmntfile") or "").strip()
    if url and not url.startswith("http"):
        url = f"https://www.nseindia.com{url}" if url.startswith("/") else ""
    return {
        "title": title,
        "url": url or "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        "summary": attachment or subject,
        "source": "nse_corporate_announcements",
        "published_at": ts,
    }


def fetch_nse_bulk_block_deals(
    symbol: str,
    *,
    days: int = 7,
    timeout_seconds: float = 20.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"items": [], "error": "missing_symbol"}
    from_date, to_date = _nse_date_range(days, now_utc=now_utc)
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for deal_type in ("bulk_deals", "block_deals"):
        payload, err = _fetch_nse_api(
            _NSE_BULK_BLOCK_URL,
            params={"optionType": deal_type, "from": from_date, "to": to_date},
            timeout_seconds=timeout_seconds,
        )
        if payload is None:
            errors.append(f"{deal_type}:{err or 'unknown'}")
            continue
        rows: list[Any]
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        else:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_sym = str(row.get("BD_SYMBOL") or row.get("symbol") or "").strip().upper()
            if row_sym != sym:
                continue
            normalized = _normalize_nse_deal_row(row, deal_type=deal_type)
            if normalized is not None:
                items.append(normalized)
    if items:
        return {"items": items, "error": ""}
    if errors:
        return {"items": [], "error": errors[0]}
    return {"items": [], "error": "nse_empty"}


def fetch_nse_corporate_announcements(
    symbol: str,
    *,
    days: int = 30,
    timeout_seconds: float = 20.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"items": [], "error": "missing_symbol"}
    from_date, to_date = _nse_date_range(days, now_utc=now_utc)
    payload, err = _fetch_nse_api(
        _NSE_CORPORATE_ANNOUNCEMENTS_URL,
        params={
            "index": "equities",
            "symbol": sym,
            "from_date": from_date,
            "to_date": to_date,
        },
        timeout_seconds=timeout_seconds,
    )
    if payload is None:
        return {"items": [], "error": err or "nse_request_error"}
    rows = payload if isinstance(payload, list) else []
    items: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_nse_announcement_row(row)
        if normalized is not None:
            items.append(normalized)
    if items:
        return {"items": items, "error": ""}
    return {"items": [], "error": "nse_empty"}


def _resolve_stock_news_fetch_error(
    *,
    final_items: list[dict[str, Any]],
    pre_filter_count: int,
    google_rss_found: bool,
    last_error: str,
    nse_errors: list[str],
) -> str:
    if final_items:
        return ""
    if pre_filter_count > 0:
        return "all_filtered"
    if google_rss_found:
        return "all_filtered"
    if last_error and last_error != "empty_feed":
        return last_error
    if nse_errors:
        return nse_errors[0]
    return last_error or "empty_feed"


def fetch_stock_news_for_symbol(
    cfg: TitanConfig,
    *,
    symbol: str,
    exchange: str,
    timeout_seconds: float = 10.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    sym = str(symbol or "").strip().upper()
    aliases = _instrument_alias_candidates(cfg, symbol=sym, exchange=exchange)
    primary_queries, fallback_queries = _stock_news_query_candidates(symbol=sym, aliases=aliases)
    max_items = _stock_news_fetch_limit()
    last_error = ""
    used_query = ""
    used_alias = ""
    query_fallback_used = False
    nse_errors: list[str] = []
    raw_items: list[dict[str, Any]] = []
    google_rss_found = False
    if _stock_news_enable_nse() and str(exchange or "").strip().upper() == "NSE":
        bulk_meta = fetch_nse_bulk_block_deals(
            sym,
            days=7,
            timeout_seconds=timeout_seconds,
            now_utc=now,
        )
        if bulk_meta.get("items"):
            raw_items.extend(bulk_meta["items"])
        elif bulk_meta.get("error"):
            nse_errors.append(f"bulk_deals:{bulk_meta['error']}")
        ann_meta = fetch_nse_corporate_announcements(
            sym,
            days=30,
            timeout_seconds=timeout_seconds,
            now_utc=now,
        )
        if ann_meta.get("items"):
            raw_items.extend(ann_meta["items"])
        elif ann_meta.get("error"):
            nse_errors.append(f"announcements:{ann_meta['error']}")
    google_items, used_query, used_alias, last_error = _fetch_google_news_rss_items(
        primary_queries,
        timeout_seconds=timeout_seconds,
        symbol=sym,
    )
    if google_items:
        google_rss_found = True
        raw_items.extend(google_items)
    elif fallback_queries:
        fallback_items, fb_query, fb_alias, fb_error = _fetch_google_news_rss_items(
            fallback_queries,
            timeout_seconds=timeout_seconds,
            symbol=sym,
        )
        if fallback_items:
            google_rss_found = True
            query_fallback_used = True
            raw_items.extend(fallback_items)
            if fb_query:
                used_query = fb_query
            if fb_alias:
                used_alias = fb_alias
            last_error = ""
        elif fb_error:
            last_error = fb_error
    normalized = _dedupe_recent_news_items(raw_items, now_utc=now, limit=max_items)
    filter_input: list[dict[str, Any]] = []
    for item in normalized:
        ts_raw = item.get("published_at")
        ts = _parse_news_timestamp(str(ts_raw)) if not isinstance(ts_raw, datetime) else ts_raw
        filter_input.append(
            {
                **item,
                "published_at": ts if ts is not None else now,
            }
        )
    filtered_items, filter_meta = _filter_stock_news_items(
        symbol=sym,
        aliases=aliases,
        items=filter_input,
    )
    final_items: list[dict[str, Any]] = []
    for item in filtered_items[:max_items]:
        ts = item.get("published_at")
        if isinstance(ts, datetime):
            ts_out = ts.isoformat()
        else:
            ts_out = str(ts or "").strip()
        final_items.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "summary": str(item.get("summary", "")).strip(),
                "source": str(item.get("source", "unknown_source")).strip() or "unknown_source",
                "published_at": ts_out,
            }
        )
    fetch_error = _resolve_stock_news_fetch_error(
        final_items=final_items,
        pre_filter_count=len(filter_input),
        google_rss_found=google_rss_found,
        last_error=last_error,
        nse_errors=nse_errors,
    )
    return {
        "symbol": sym,
        "exchange": str(exchange).strip().upper(),
        "items": final_items,
        "query_used": used_query,
        "alias_used": used_alias,
        "fallback_used": bool(used_alias) or query_fallback_used,
        "error": fetch_error,
        "filtered_count": int(filter_meta.get("filtered_count") or 0),
        "rejection_samples": filter_meta.get("rejection_samples") or [],
        "relevance_top_score": filter_meta.get("relevance_top_score"),
        "nse_errors": nse_errors,
        "aliases": aliases,
        "rss_pre_filter_count": len(filter_input),
    }


def _news_sentiment_score(text: str, *, stock_path: bool = False) -> float:
    t = _normalize_news_text(text)
    if not t:
        return 0.0
    pos_terms = set(_POSITIVE_NEWS_TERMS)
    if stock_path:
        pos_terms.discard("upgrade")
    pos_hits = sum(1 for k in pos_terms if k in t)
    if stock_path and "upgrade" in t and any(k in t for k in _STOCK_NEWS_RECO_CONTEXT_TERMS):
        pos_hits -= 1
    neg_hits = sum(1 for k in _NEGATIVE_NEWS_TERMS if k in t)
    raw = (pos_hits - neg_hits) / max(2.0, pos_hits + neg_hits + 1.0)
    return round(_clamp(raw, -1.0, 1.0), 4)


def _stock_news_confidence_score(item: dict[str, Any], *, relevance: float) -> float:
    source = str(item.get("source", "")).lower()
    url = str(item.get("url", "")).lower()
    blob = f"{source} {url}"
    conf = 0.45
    if source in _STOCK_NEWS_NSE_SOURCES:
        conf = 0.90
    elif any(fragment in blob for fragment in _stock_news_quality_source_fragments()):
        conf = 0.75
    elif any(fragment in blob for fragment in _STOCK_NEWS_AGGREGATOR_FRAGMENTS):
        conf = 0.35
    if str(item.get("url", "")).strip():
        conf += 0.05
    conf += relevance * 0.15
    return round(_clamp(conf, 0.2, 1.0), 4)


def _news_impact_score(text: str) -> float:
    t = _normalize_news_text(text)
    base = 0.25
    for term, delta in _IMPACT_NEWS_TERMS.items():
        if term in t:
            base += delta
    if len(t) > 180:
        base += 0.1
    return round(_clamp(base, 0.05, 1.0), 4)


def _news_confidence_score(item: dict[str, Any]) -> float:
    source = str(item.get("source", "")).lower()
    conf = 0.45
    if source:
        conf += 0.15
    if str(item.get("url", "")).strip():
        conf += 0.1
    if "bbc" in source or "nyt" in source or "reuters" in source or "aljazeera" in source:
        conf += 0.15
    return round(_clamp(conf, 0.2, 1.0), 4)


def _theme_hits_for_sector(text: str, sector_key: str) -> float:
    terms = _SECTOR_THEME_KEYWORDS.get(sector_key.strip().lower(), ())
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t in text)
    if hits <= 0:
        return 0.0
    return round(min(2.0, 1.0 + (hits - 1) * 0.25), 4)


def score_sector_news(news_items: list[dict[str, Any]], *, sector_key: str) -> dict[str, Any]:
    drivers: list[dict[str, Any]] = []
    contribution_total = 0.0
    abs_weight_total = 0.0
    sector = sector_key.strip().lower()
    for item in news_items:
        title = str(item.get("title", "")).strip()
        summary = str(item.get("summary", "")).strip()
        text = _normalize_news_text(f"{title} {summary}")
        theme_weight = _theme_hits_for_sector(text, sector)
        if theme_weight <= 0.0:
            continue
        sentiment = _news_sentiment_score(text)
        impact = _news_impact_score(text)
        confidence = _news_confidence_score(item)
        contribution = theme_weight * sentiment * impact * confidence
        abs_weight = theme_weight * impact
        if contribution > 0.02:
            direction = "tailwind"
        elif contribution < -0.02:
            direction = "headwind"
        else:
            direction = "neutral"
        contribution_total += contribution
        abs_weight_total += abs_weight
        drivers.append(
            {
                "title": title,
                "source": str(item.get("source", "")).strip() or "unknown_source",
                "published_at": str(item.get("published_at", "")).strip(),
                "sentiment": round(sentiment, 4),
                "impact": round(impact, 4),
                "confidence": round(confidence, 4),
                "contribution": round(contribution, 4),
                "url": str(item.get("url", "")).strip(),
                "driver": title,
                "affected_metric": "rank_score",
                "affected_theme": sector,
                "direction": direction,
            }
        )
    normalized_score = 0.0 if abs_weight_total <= 0.0 else (contribution_total / abs_weight_total)
    normalized_score = round(_clamp(normalized_score, -1.0, 1.0), 4)
    drivers_sorted = sorted(drivers, key=lambda d: abs(_safe_float(d.get("contribution"))), reverse=True)
    limit = _news_driver_limit()
    top = drivers_sorted[:limit]
    boosts = [d for d in top if _safe_float(d.get("contribution")) > 0]
    drags = [d for d in top if _safe_float(d.get("contribution")) < 0]
    conf = 0.0
    if top:
        conf = sum(max(0.0, _safe_float(d.get("confidence"))) for d in top) / len(top)
    return {
        "sector_key": sector,
        "score": normalized_score,
        "confidence": round(_clamp(conf, 0.0, 1.0), 4),
        "drivers_top": top,
        "drivers_boosting": boosts,
        "drivers_dragging": drags,
        "matched_items": len(drivers),
    }


def map_news_to_sector_scores(news_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sector_key in sorted(_SECTOR_THEME_KEYWORDS.keys()):
        out[sector_key] = score_sector_news(news_items, sector_key=sector_key)
    return out


def _macro_news_scope(*, title: str, source: str) -> str:
    txt = _normalize_news_text(f"{title} {source}")
    local_terms = (
        "india",
        "indian",
        "nse",
        "bse",
        "nifty",
        "sensex",
        "rbi",
        "sebi",
        "rupee",
        "moneycontrol",
        "livemint",
        "economictimes",
        "business standard",
    )
    market_terms = (
        "stock",
        "stocks",
        "shares",
        "equity",
        "earnings",
        "guidance",
        "ipo",
        "buyback",
        "merger",
        "acquisition",
    )
    if any(term in txt for term in local_terms):
        return "local"
    if any(term in txt for term in market_terms):
        return "market"
    return "global"


def build_macro_news_layers(snapshot: dict[str, Any], *, sector_key: str) -> dict[str, list[dict[str, Any]]]:
    layers: dict[str, list[dict[str, Any]]] = {"global": [], "local": [], "market": []}
    scores = snapshot.get("sector_scores")
    score_map = scores if isinstance(scores, dict) else {}
    sector = str(sector_key or "").strip().lower()
    sector_row = score_map.get(sector) if isinstance(score_map.get(sector), dict) else {}
    drivers = sector_row.get("drivers_top") if isinstance(sector_row.get("drivers_top"), list) else []
    if not drivers:
        fallback_drivers: list[dict[str, Any]] = []
        for row in score_map.values():
            if not isinstance(row, dict):
                continue
            drows = row.get("drivers_top")
            if not isinstance(drows, list):
                continue
            for d in drows:
                if isinstance(d, dict):
                    fallback_drivers.append(d)
        drivers = sorted(
            fallback_drivers,
            key=lambda d: abs(_safe_float(d.get("contribution"))),
            reverse=True,
        )[: max(1, _news_driver_limit())]
    for raw in drivers:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("driver") or "").strip()
        if not title:
            continue
        scope = _macro_news_scope(title=title, source=str(raw.get("source") or ""))
        layers.setdefault(scope, []).append(
            {
                "headline": title,
                "source": str(raw.get("source") or "unknown_source").strip() or "unknown_source",
                "published_at": str(raw.get("published_at") or "").strip(),
                "impact_contribution_score": round(_safe_float(raw.get("contribution")), 4),
            }
        )
    return layers


def correlate_stock_news_with_macro(
    *,
    symbol: str,
    sector_key: str,
    stock_news_items: list[dict[str, Any]],
    snapshot: dict[str, Any],
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    alias_list = aliases if isinstance(aliases, list) else []
    filtered_items, filter_meta = _filter_stock_news_items(
        symbol=symbol,
        aliases=alias_list,
        items=stock_news_items,
    )
    stock_rows: list[dict[str, Any]] = []
    stock_contribution = 0.0
    stock_weight = 0.0
    min_relevance = _stock_news_min_relevance()
    for item in filtered_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title:
            continue
        text = _normalize_news_text(f"{title} {summary}")
        relevance = _safe_float(item.get("relevance_score"))
        if math.isnan(relevance):
            relevance = _stock_news_relevance_score(symbol=symbol, aliases=alias_list, item=item)
        if relevance < min_relevance:
            continue
        sentiment = _news_sentiment_score(text, stock_path=True)
        impact = _news_impact_score(text)
        confidence = _stock_news_confidence_score(item, relevance=relevance)
        contribution = sentiment * impact * confidence * relevance
        stock_contribution += contribution
        stock_weight += max(impact * relevance, 0.05)
        stock_rows.append(
            {
                "headline": title,
                "source": str(item.get("source") or "unknown_source").strip() or "unknown_source",
                "published_at": str(item.get("published_at") or "").strip(),
                "impact_contribution_score": round(contribution, 4),
                "relevance_score": round(relevance, 4),
            }
        )
    stock_rows = sorted(
        stock_rows,
        key=lambda x: abs(_safe_float(x.get("impact_contribution_score"))),
        reverse=True,
    )[: max(1, _news_driver_limit())]
    stock_score = 0.0 if stock_weight <= 0.0 else stock_contribution / stock_weight
    scores = snapshot.get("sector_scores")
    score_map = scores if isinstance(scores, dict) else {}
    sector = str(sector_key or "").strip().lower()
    sector_row = score_map.get(sector) if isinstance(score_map.get(sector), dict) else {}
    macro_score = _safe_float(sector_row.get("score"))
    if math.isnan(macro_score):
        macro_score = 0.0
    macro_conf = _safe_float(sector_row.get("confidence"))
    if math.isnan(macro_conf):
        macro_conf = 0.35
    if stock_rows:
        blended = (stock_score * 0.7) + (macro_score * 0.3)
        confidence = _clamp((0.65 * 0.7) + (macro_conf * 0.3), 0.2, 0.95)
    else:
        blended = macro_score
        confidence = _clamp(macro_conf, 0.2, 0.9)
    if blended > 0.02:
        direction = "tailwind"
    elif blended < -0.02:
        direction = "headwind"
    else:
        direction = "neutral"
    macro_layers = build_macro_news_layers(snapshot, sector_key=sector)
    evidence_layers = {
        "global": macro_layers.get("global", [])[:2],
        "local": macro_layers.get("local", [])[:2],
        "market": macro_layers.get("market", [])[:2],
        "stock": stock_rows[:2],
    }
    stock_driver = stock_rows[0] if stock_rows else {}
    if stock_driver:
        driver = f"{stock_driver.get('headline')} ({stock_driver.get('source')})"
        fallback_label = ""
    else:
        macro_driver = (macro_layers.get("market") or macro_layers.get("local") or macro_layers.get("global") or [{}])[0]
        driver_headline = str(macro_driver.get("headline") or "No recent market driver available").strip()
        driver_source = str(macro_driver.get("source") or "snapshot_unavailable").strip()
        driver = f"{driver_headline} ({driver_source})"
        if stock_news_items and not stock_rows:
            fallback_label = "stock_news_no_relevant_items"
        elif driver_headline == "No recent market driver available":
            fallback_label = "sector_specific_match_missing_no_market_driver"
        else:
            scope = _macro_news_scope(title=driver_headline, source=driver_source)
            fallback_label = f"sector_specific_match_missing_using_{scope}_market_driver"
    return {
        "symbol": str(symbol or "").strip().upper(),
        "driver": driver,
        "direction": direction,
        "confidence": round(float(confidence), 4),
        "sector_news_score": round(float(macro_score), 4),
        "stock_news_score": round(float(stock_score), 4),
        "net_score": round(float(blended), 4),
        "fallback_label": fallback_label,
        "filtered_count": int(filter_meta.get("filtered_count") or 0),
        "rejection_samples": filter_meta.get("rejection_samples") or [],
        "relevance_top_score": filter_meta.get("relevance_top_score"),
        "evidence": {
            "net_news_impact_score": round(float(blended), 4),
            "net_news_impact_direction": direction,
            "top_headlines": evidence_layers,
            "macro_layer_scores": {
                "global_count": len(evidence_layers.get("global") or []),
                "local_count": len(evidence_layers.get("local") or []),
                "market_count": len(evidence_layers.get("market") or []),
                "stock_count": len(evidence_layers.get("stock") or []),
            },
        },
    }


def _news_blend_points(sector_news_score: float) -> float:
    points = sector_news_score * _news_blend_weight()
    return round(_clamp(points, -_news_blend_cap(), _news_blend_cap()), 4)


def _to_utc_datetime(raw: Any) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_latest_news_snapshot(cfg: TitanConfig) -> dict[str, Any] | None:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    table_name = _news_snapshot_table_name()
    try:
        res = (
            client.table(table_name)
            .select("refreshed_at,item_count,fetch_status,refresh_error,news_items,sector_scores")
            .order("refreshed_at", desc=True)
            .limit(1)
            .execute()
        )
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        logger.info("Global news snapshot read skipped (table missing or unavailable): %s", msg)
        return None
    except Exception as exc:
        logger.info("Global news snapshot read skipped: %s", exc)
        return None
    rows = list(getattr(res, "data", None) or [])
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    refreshed_at = _to_utc_datetime(row.get("refreshed_at"))
    if refreshed_at is None:
        return None
    news_items = row.get("news_items")
    if not isinstance(news_items, list):
        news_items = []
    sector_scores = row.get("sector_scores")
    if not isinstance(sector_scores, dict):
        sector_scores = map_news_to_sector_scores(news_items)
    return {
        "refreshed_at": refreshed_at.isoformat(),
        "item_count": int(row.get("item_count") or len(news_items)),
        "fetch_status": str(row.get("fetch_status") or "ok"),
        "refresh_error": str(row.get("refresh_error") or "").strip(),
        "news_items": news_items,
        "sector_scores": sector_scores,
    }


def refresh_global_news_snapshot(
    cfg: TitanConfig,
    *,
    timeout_seconds: float = 12.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    # Keep call signature stable for tests that monkeypatch fetch_latest_global_news without kwargs.
    news_items = fetch_latest_global_news()
    sector_scores = map_news_to_sector_scores(news_items)
    row = {
        "refreshed_at": now.isoformat(),
        "item_count": int(len(news_items)),
        "fetch_status": "ok" if news_items else "empty",
        "refresh_error": "",
        "news_items": news_items,
        "sector_scores": sector_scores,
    }
    persisted = False
    persist_reason = "disabled"
    table_name = _news_snapshot_table_name()
    try:
        client = create_client(cfg.supabase_url, cfg.supabase_key)
        client.table(table_name).insert(row).execute()
        persisted = True
        persist_reason = "ok"
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        persist_reason = "missing_table" if "could not find the table" in msg.lower() else "api_error"
        logger.info("Global news snapshot persist skipped (%s): %s", persist_reason, msg)
    except Exception as exc:
        persist_reason = "unexpected"
        logger.info("Global news snapshot persist skipped: %s", exc)
    return {
        "source": "refreshed",
        "refreshed_at": row["refreshed_at"],
        "item_count": row["item_count"],
        "fetch_status": row["fetch_status"],
        "refresh_error": row["refresh_error"],
        "ttl_hours": _news_snapshot_ttl_hours(),
        "age_minutes": 0.0,
        "fresh": True,
        "persisted": persisted,
        "persist_reason": persist_reason,
        "news_items": news_items,
        "sector_scores": sector_scores,
    }


def resolve_global_news_snapshot(
    cfg: TitanConfig,
    *,
    force_refresh: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    ttl_hours = _news_snapshot_ttl_hours()
    cached = _load_latest_news_snapshot(cfg)
    if cached:
        ref_dt = _to_utc_datetime(cached.get("refreshed_at"))
        age_minutes = (
            max(0.0, (now - ref_dt).total_seconds() / 60.0)
            if isinstance(ref_dt, datetime)
            else float("inf")
        )
        cached_out = {
            "source": "cached",
            "ttl_hours": ttl_hours,
            "age_minutes": round(age_minutes, 3),
            "fresh": bool(age_minutes <= (ttl_hours * 60.0)),
            **cached,
        }
        if cached_out["fresh"] and not force_refresh:
            return cached_out
    else:
        cached_out = None
    try:
        refreshed = refresh_global_news_snapshot(cfg, now_utc=now)
        refreshed["fresh"] = True
        refreshed["age_minutes"] = 0.0
        return refreshed
    except Exception as exc:
        logger.warning("Global news refresh failed; attempting stale fallback: %s", exc)
        if cached_out:
            cached_out["source"] = "stale_fallback"
            cached_out["fresh"] = False
            cached_out["refresh_error"] = str(exc)
            return cached_out
        return {
            "source": "unavailable",
            "ttl_hours": ttl_hours,
            "age_minutes": float("inf"),
            "fresh": False,
            "refreshed_at": None,
            "item_count": 0,
            "fetch_status": "error",
            "refresh_error": str(exc),
            "persisted": False,
            "persist_reason": "not_attempted",
            "news_items": [],
            "sector_scores": {},
        }


def _bucket_from_market_cap_cr(market_cap_inr_cr: float | None) -> str:
    if market_cap_inr_cr is None:
        return "unknown"
    if market_cap_inr_cr < 5_000.0:
        return "micro"
    if market_cap_inr_cr < 20_000.0:
        return "small"
    if market_cap_inr_cr < 50_000.0:
        return "mid"
    return "large"


def _cap_bias(bucket: str) -> float:
    if bucket == "micro":
        return 8.0
    if bucket == "small":
        return 6.0
    if bucket == "mid":
        return 3.0
    if bucket == "large":
        return 1.0
    return 0.0


def _build_nse_payload_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/",
        },
    )


def _http_get_text(url: str, *, timeout_seconds: float = 20.0) -> tuple[str | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*,text/html",
            "Referer": "https://www.google.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return None, f"http_{int(getattr(e, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, "request_error"


def _fetch_nse_json(symbol: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if not sym:
        return {}
    # Warm cookie/session.
    try:
        urllib.request.urlopen(
            _build_nse_payload_request(_NSE_HOME_URL),
            timeout=timeout_seconds,
        ).read()
    except Exception:
        return {}
    try:
        with urllib.request.urlopen(
            _build_nse_payload_request(_NSE_QUOTE_URL.format(symbol=sym)),
            timeout=timeout_seconds,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_nse_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    payload = _fetch_nse_json(symbol)
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    raw = info.get("marketCap")
    v = _safe_float(raw)
    if math.isnan(v) or v <= 0.0:
        return None, "nse_quote_missing"
    # NSE marketCap is commonly rupees. Convert to INR crore.
    # 1 crore = 10,000,000 INR.
    if v > 10_000_000.0:
        return round(v / 10_000_000.0, 2), "nse_quote_rupees"
    # Defensive fallback for already-crore values.
    return round(v, 2), "nse_quote_crore"


def fetch_moneycontrol_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    query = urllib.parse.quote(symbol.strip().upper())
    raw, err = _http_get_text(_MONEYCONTROL_SUGGEST_URL.format(query=query))
    if raw is None:
        return None, f"moneycontrol_suggest_{err}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "moneycontrol_suggest_invalid_json"
    items = payload if isinstance(payload, list) else []
    link_src = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        s = str(item.get("sc_id", "")).strip().upper()
        if s == symbol.strip().upper():
            link_src = str(item.get("link_src", "")).strip()
            break
        if not link_src:
            link_src = str(item.get("link_src", "")).strip()
    if not link_src:
        return None, "moneycontrol_suggest_missing_link"
    page_raw, page_err = _http_get_text(link_src)
    if page_raw is None:
        return None, f"moneycontrol_quote_{page_err}"
    m = re.search(
        r"Mkt Cap \(Rs\. Cr\.\)\s*</td>\s*<td[^>]*>\s*([0-9,]+(?:\.[0-9]+)?)\s*</td>",
        page_raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, "moneycontrol_quote_missing_cap"
    val = _safe_float(m.group(1).replace(",", ""))
    if math.isnan(val) or val <= 0.0:
        return None, "moneycontrol_quote_invalid_cap"
    return round(val, 2), "moneycontrol_quote_rs_cr"


def fetch_screener_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    query = urllib.parse.quote(symbol.strip().upper())
    raw, err = _http_get_text(_SCREENER_SEARCH_URL.format(query=query))
    if raw is None:
        return None, f"screener_search_{err}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "screener_search_invalid_json"
    rows = payload if isinstance(payload, list) else []
    comp_url = ""
    for item in rows:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name", "")).strip().upper()
        if n == symbol.strip().upper():
            comp_url = str(item.get("url", "")).strip()
            break
        if not comp_url:
            comp_url = str(item.get("url", "")).strip()
    if not comp_url:
        return None, "screener_search_missing_url"
    url = f"https://www.screener.in{comp_url}" if comp_url.startswith("/") else comp_url
    page_raw, page_err = _http_get_text(url)
    if page_raw is None:
        return None, f"screener_quote_{page_err}"
    m = re.search(
        r"Market Cap</span>\s*<span class=\"number\">\s*([0-9,]+(?:\.[0-9]+)?)\s*</span>\s*Cr\.",
        page_raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, "screener_quote_missing_cap"
    val = _safe_float(m.group(1).replace(",", ""))
    if math.isnan(val) or val <= 0.0:
        return None, "screener_quote_invalid_cap"
    return round(val, 2), "screener_quote_rs_cr"


def _yahoo_ticker(symbol: str, exchange: str) -> str:
    ex = exchange.strip().upper()
    if ex == "NSE":
        return f"{symbol}.NS"
    if ex == "BSE":
        return f"{symbol}.BO"
    return symbol


def fetch_yahoo_market_cap_inr_cr(symbol: str, exchange: str) -> tuple[float | None, str]:
    ticker = _yahoo_ticker(symbol.strip().upper(), exchange)
    url = _YAHOO_QUOTE_URL.format(symbols=urllib.parse.quote(ticker))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"yahoo_quote_http_{int(getattr(e, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, "yahoo_quote_error"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "yahoo_quote_invalid_json"
    qr = payload.get("quoteResponse") if isinstance(payload, dict) else {}
    result = qr.get("result") if isinstance(qr, dict) else []
    first = result[0] if isinstance(result, list) and result else {}
    if not isinstance(first, dict):
        return None, "yahoo_quote_missing"
    market_cap = _safe_float(first.get("marketCap"))
    if math.isnan(market_cap) or market_cap <= 0.0:
        return None, "yahoo_quote_missing"
    currency = str(first.get("currency", "")).strip().upper()
    if currency and currency != "INR":
        return None, f"yahoo_quote_currency_{currency.lower()}"
    return round(market_cap / 10_000_000.0, 2), "yahoo_quote_rupees"


def _load_previous_market_caps(
    cfg: TitanConfig,
    *,
    sector_key: str,
) -> dict[tuple[str, str], tuple[float, str]]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("sector_priority_rankings")
            .select("symbol,exchange,market_cap_inr_cr,market_cap_bucket")
            .eq("sector_key", sector_key)
            .order("as_of_date", desc=True)
            .limit(1000)
            .execute()
        )
    except Exception:
        return {}
    rows = list(getattr(res, "data", None) or [])
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        ex = str(row.get("exchange", "")).strip().upper()
        cap = _safe_float(row.get("market_cap_inr_cr"))
        bucket = str(row.get("market_cap_bucket", "")).strip().lower()
        if not sym or ex not in ("NSE", "BSE"):
            continue
        if math.isnan(cap) or cap <= 0.0:
            continue
        key = (sym, ex)
        if key not in out:
            out[key] = (cap, bucket if bucket else _bucket_from_market_cap_cr(cap))
    return out


def _return_pct(series: pd.Series, periods_back: int) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= periods_back:
        return float("nan")
    prev = float(s.iloc[-(periods_back + 1)])
    last = float(s.iloc[-1])
    if prev == 0.0:
        return float("nan")
    return ((last / prev) - 1.0) * 100.0


def _score_from_features(*, bucket: str, ret_1w: float, ret_1m: float, absorption: float) -> float:
    ret_1w_term = 0.0 if math.isnan(ret_1w) else (ret_1w * 1.1)
    ret_1m_term = 0.0 if math.isnan(ret_1m) else (ret_1m * 0.45)
    absorption_term = 0.0 if math.isnan(absorption) else ((absorption - 1.0) * 8.0)
    score = _cap_bias(bucket) + ret_1w_term + ret_1m_term + absorption_term
    return round(score, 4)


def build_sector_rankings(
    cfg: TitanConfig,
    *,
    sector_key: str,
    instruments: list[SectorInstrument],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    from breeze_client import create_breeze_session

    breeze = create_breeze_session(cfg)
    as_of_date = datetime.now(IST).date().isoformat()
    prev_caps = _load_previous_market_caps(cfg, sector_key=sector_key)
    snapshot = resolve_global_news_snapshot(cfg)
    global_news = snapshot.get("news_items") if isinstance(snapshot.get("news_items"), list) else []
    sector_news_scores = (
        snapshot.get("sector_scores")
        if isinstance(snapshot.get("sector_scores"), dict)
        else map_news_to_sector_scores(global_news)
    )
    sector_news = sector_news_scores.get(
        sector_key.strip().lower(),
        {
            "score": 0.0,
            "confidence": 0.0,
            "drivers_top": [],
            "drivers_boosting": [],
            "drivers_dragging": [],
            "matched_items": 0,
        },
    )
    sector_news_score = _safe_float(sector_news.get("score"))
    if math.isnan(sector_news_score):
        sector_news_score = 0.0
    blend_points = _news_blend_points(sector_news_score)
    rows: list[dict[str, Any]] = []
    for inst in instruments:
        issues: list[str] = []
        try:
            df = fetch_equity_data(
                cfg,
                inst.symbol,
                inst.exchange,
                breeze=breeze,
                lookback_calendar_days=90,
                max_retries=2,
            )
        except Exception as exc:
            logger.warning("Ranking data fetch failed for %s (%s): %s", inst.symbol, inst.exchange, exc)
            df = pd.DataFrame()
            issues.append("price_history_fetch_error")
        close_col = "close" if "close" in df.columns else (df.columns[-1] if len(df.columns) > 0 else None)
        series = pd.to_numeric(df[close_col], errors="coerce") if close_col is not None else pd.Series(dtype=float)
        ret_1w = _return_pct(series, periods_back=5)
        ret_1m = _return_pct(series, periods_back=20)
        absorption = volume_participation_ratio(df) if not df.empty else float("nan")
        market_cap_cr, market_cap_source = fetch_nse_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_moneycontrol_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_screener_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_yahoo_market_cap_inr_cr(inst.symbol, inst.exchange)
        if market_cap_cr is None:
            prev = prev_caps.get((inst.symbol, inst.exchange))
            if prev is not None:
                market_cap_cr = round(float(prev[0]), 2)
                market_cap_source = "prior_snapshot"
        bucket = _bucket_from_market_cap_cr(market_cap_cr)
        if market_cap_cr is None:
            issues.append("market_cap_missing")
        if df.empty:
            issues.append("price_history_missing")
        if math.isnan(ret_1w):
            issues.append("return_1w_missing")
        if math.isnan(ret_1m):
            issues.append("return_1m_missing")
        if math.isnan(absorption):
            issues.append("absorption_missing")
        base_score = _score_from_features(
            bucket=bucket,
            ret_1w=ret_1w,
            ret_1m=ret_1m,
            absorption=absorption,
        )
        score = round(base_score + blend_points, 4)
        news_meta: dict[str, Any] = {
            "fetched_count": len(global_news),
            "snapshot_source": snapshot.get("source"),
            "snapshot_refreshed_at": snapshot.get("refreshed_at"),
            "snapshot_ttl_hours": snapshot.get("ttl_hours"),
            "snapshot_age_minutes": snapshot.get("age_minutes"),
            "snapshot_is_fresh": bool(snapshot.get("fresh")),
            "snapshot_fetch_status": snapshot.get("fetch_status"),
            "snapshot_refresh_error": snapshot.get("refresh_error"),
            "matched_items": int(sector_news.get("matched_items") or 0),
            "sector_news_score": round(sector_news_score, 4),
            "blend_points": blend_points,
            "blend_weight": _news_blend_weight(),
            "blend_cap": _news_blend_cap(),
            "confidence": round(_safe_float(sector_news.get("confidence")), 4),
            "drivers_boosting": sector_news.get("drivers_boosting") or [],
            "drivers_dragging": sector_news.get("drivers_dragging") or [],
            "drivers_top": sector_news.get("drivers_top") or [],
        }
        if not global_news:
            news_meta["reason"] = "news_unavailable"
        elif int(sector_news.get("matched_items") or 0) <= 0:
            news_meta["reason"] = "no_sector_news_match"
        if snapshot.get("source") == "stale_fallback":
            news_meta["reason"] = "stale_snapshot_fallback"
        rows.append(
            {
                "sector_key": sector_key,
                "symbol": inst.symbol,
                "exchange": inst.exchange,
                "as_of_date": as_of_date,
                "market_cap_inr_cr": market_cap_cr,
                "market_cap_bucket": bucket,
                "return_1w_pct": _round_or_none(ret_1w, digits=3),
                "return_1m_pct": _round_or_none(ret_1m, digits=3),
                "absorption_ratio": _round_or_none(absorption, digits=4),
                "rank_score": score,
                "meta": {
                    "market_cap_source": market_cap_source,
                    "rows_count": int(len(df)),
                    "issues": sorted(set(issues)),
                    "technical_rank_score": base_score,
                    "news": news_meta,
                },
            }
        )
    ranked = sorted(
        rows,
        key=lambda r: (_safe_float(r.get("rank_score")), _safe_float(r.get("return_1w_pct"))),
        reverse=True,
    )
    top_n = max(1, int(top_n))
    priority_candidates = [r for r in ranked if int((r.get("meta") or {}).get("rows_count") or 0) > 0]
    # Primary objective: small/micro-cap AI names for higher-move opportunity.
    preferred = [r for r in priority_candidates if str(r.get("market_cap_bucket")) in ("micro", "small")]
    fallback = [r for r in priority_candidates if str(r.get("market_cap_bucket")) not in ("micro", "small")]
    ordered_candidates = preferred + fallback
    priority_keys = {
        (r["symbol"], r["exchange"])
        for r in ordered_candidates[:top_n]
    }
    for i, row in enumerate(ranked, start=1):
        row["rank_in_sector"] = i
        row["is_priority"] = (row["symbol"], row["exchange"]) in priority_keys
    return ranked


def persist_sector_rankings(cfg: TitanConfig, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"persisted": False, "reason": "no_rows"}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        sector_key = str(rows[0].get("sector_key", "")).strip().lower()
        as_of_date = str(rows[0].get("as_of_date", "")).strip()
        if sector_key and as_of_date:
            client.table("sector_priority_rankings").delete().eq("sector_key", sector_key).eq(
                "as_of_date", as_of_date
            ).execute()
        client.table("sector_priority_rankings").upsert(
            rows,
            on_conflict="sector_key,symbol,exchange,as_of_date",
        ).execute()
        return {"persisted": True, "rows": len(rows)}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return {"persisted": False, "reason": "missing_table", "message": msg}
        return {"persisted": False, "reason": "api_error", "message": msg}


def load_priority_instruments(
    cfg: TitanConfig,
    *,
    sector_key: str,
    top_n: int | None = None,
) -> list[SectorInstrument]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of = datetime.now(IST).date().isoformat()
    q = (
        client.table("sector_priority_rankings")
        .select("symbol,exchange,rank_in_sector")
        .eq("sector_key", sector_key)
        .eq("as_of_date", as_of)
        .eq("is_priority", True)
        .order("rank_in_sector")
    )
    if top_n is not None:
        q = q.limit(max(1, int(top_n)))
    try:
        res = q.execute()
    except Exception as exc:
        logger.warning("Priority load failed for sector=%s: %s", sector_key, exc)
        return []
    data = list(getattr(res, "data", None) or [])
    out: list[SectorInstrument] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        ex = str(row.get("exchange", "")).strip().upper()
        if not sym or ex not in ("NSE", "BSE"):
            continue
        out.append(SectorInstrument(symbol=sym, exchange=ex))
    return out


def persist_daily_winners(
    cfg: TitanConfig,
    *,
    sector_key: str,
    top_n: int = 10,
) -> dict[str, Any]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of = datetime.now(IST).date().isoformat()
    q = (
        client.table("sector_priority_rankings")
        .select(
            "symbol,exchange,rank_score,market_cap_bucket,return_1w_pct,return_1m_pct,absorption_ratio,meta,rank_in_sector,is_priority"
        )
        .eq("sector_key", sector_key)
        .eq("as_of_date", as_of)
        .eq("is_priority", True)
        .order("rank_in_sector")
        .limit(max(1, int(top_n)))
    )
    try:
        res = q.execute()
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        return {"persisted": False, "reason": "ranking_read_failed", "message": msg}
    rows = list(getattr(res, "data", None) or [])
    if not rows:
        return {"persisted": False, "reason": "no_priority_rows"}

    to_upsert: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        issues = meta.get("issues") if isinstance(meta.get("issues"), list) else []
        to_upsert.append(
            {
                "sector_key": sector_key,
                "as_of_date": as_of,
                "winner_rank": i,
                "symbol": str(row.get("symbol", "")).strip().upper(),
                "exchange": str(row.get("exchange", "")).strip().upper(),
                "rank_score": _safe_float(row.get("rank_score")),
                "market_cap_bucket": str(row.get("market_cap_bucket", "unknown")).strip().lower() or "unknown",
                "score_breakdown": {
                    "return_1w_pct": row.get("return_1w_pct"),
                    "return_1m_pct": row.get("return_1m_pct"),
                    "absorption_ratio": row.get("absorption_ratio"),
                    "technical_rank_score": meta.get("technical_rank_score"),
                    "news_sector_score": ((meta.get("news") or {}).get("sector_news_score")),
                    "news_blend_points": ((meta.get("news") or {}).get("blend_points")),
                },
                "issue_flags": issues,
                "source_meta": {
                    "market_cap_source": meta.get("market_cap_source"),
                    "rows_count": meta.get("rows_count"),
                    "rank_in_sector": row.get("rank_in_sector"),
                    "news_drivers_boosting": ((meta.get("news") or {}).get("drivers_boosting", []))[:3],
                    "news_drivers_dragging": ((meta.get("news") or {}).get("drivers_dragging", []))[:3],
                },
            }
        )
    winners_table = client.table("sector_daily_winners")
    try:
        # Keep daily persistence idempotent even when the underlying uniqueness
        # constraint is on (sector_key, as_of_date, symbol, exchange).
        winners_table.delete().eq("sector_key", sector_key).eq("as_of_date", as_of).execute()
        winners_table.upsert(
            to_upsert,
            on_conflict="sector_key,as_of_date,winner_rank",
        ).execute()
        return {"persisted": True, "rows": len(to_upsert)}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return {"persisted": False, "reason": "missing_table", "message": msg}
        return {"persisted": False, "reason": "api_error", "message": msg}

