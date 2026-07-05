"""Sector priority ranking utilities (NSE market-cap enriched, Supabase persisted)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
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
from sector_registry import SectorInstrument, expand_symbols_with_aliases, symbol_lookup_variants
from tape_metrics import percentile_rank_0_100

logger = logging.getLogger(__name__)

_SECTOR_RELATIVE_RANK_WEIGHTS = (0.30, 0.25, 0.20, 0.15, 0.10)
_SECTOR_RELATIVE_RANK_FEATURE_KEYS = (
    "sector_pctile_return_1m",
    "sector_pctile_return_3m",
    "sector_pctile_rel_strength",
    "sector_pctile_intent",
    "sector_pctile_next_week",
)
_SECTOR_RELATIVE_MOMENTUM_WEIGHTS = (0.35, 0.25, 0.20, 0.10, 0.10)
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
    "auto": (
        "automobile",
        "passenger vehicle",
        "car sales",
        "two wheeler",
        "ev adoption",
        "auto demand",
        "vehicle production",
        "automotive sector",
    ),
    "auto_ancillary": (
        "auto component",
        "auto ancillary",
        "tyre industry",
        "oem supplier",
        "vehicle parts",
        "auto parts",
        "battery pack",
        "ancillary supplier",
    ),
    "banks_private": (
        "private bank",
        "retail lending",
        "net interest margin",
        "credit growth",
        "deposit growth",
        "loan book",
        "banking sector",
        "nim expansion",
    ),
    "banks_psu": (
        "psu bank",
        "public sector bank",
        "bank recapitalisation",
        "recapitalization",
        "slippage ratio",
        "government bank",
        "state bank",
        "psb lending",
    ),
    "capital_goods_industrials": (
        "capital goods",
        "industrial machinery",
        "heavy engineering",
        "manufacturing capex",
        "plant equipment",
        "industrial orders",
        "engineering export",
    ),
    "cement_building_materials": (
        "cement",
        "clinker",
        "building material",
        "housing demand",
        "construction activity",
        "cement price",
        "ready mix concrete",
        "ceramic tiles",
    ),
    "chemicals": (
        "specialty chemical",
        "petrochemical",
        "agrochemical",
        "chemical sector",
        "polymer demand",
        "pesticide",
        "chemical export",
    ),
    "consumer_discretionary": (
        "consumer spending",
        "retail sales",
        "discretionary demand",
        "consumer durables",
        "urban consumption",
        "lifestyle retail",
        "premium demand",
    ),
    "fmcg_staples": (
        "fmcg",
        "fast moving consumer",
        "rural demand",
        "packaged food",
        "volume growth",
        "staples demand",
        "household products",
        "consumer goods",
    ),
    "infrastructure_construction": (
        "infrastructure project",
        "highway construction",
        "road project",
        "bharatmala",
        "metro construction",
        "construction contract",
        "epc order",
        "infra capex",
    ),
    "insurance": (
        "insurance premium",
        "life insurance",
        "general insurance",
        "underwriting profit",
        "claims ratio",
        "insurance sector",
        "policy growth",
    ),
    "it": (
        "it services",
        "software export",
        "digital transformation",
        "tech spending",
        "cloud migration",
        "outsourcing deal",
        "it sector india",
    ),
    "logistics": (
        "logistics",
        "supply chain",
        "freight movement",
        "warehousing",
        "cargo volume",
        "cold chain",
        "shipping logistics",
    ),
    "media": (
        "advertising revenue",
        "broadcasting",
        "ott platform",
        "streaming service",
        "media industry",
        "content rights",
        "digital media",
    ),
    "metals_mining": (
        "steel price",
        "metal demand",
        "iron ore",
        "aluminium price",
        "copper price",
        "mining output",
        "commodity cycle",
    ),
    "nbfc_financial_services": (
        "nbfc",
        "microfinance",
        "housing finance",
        "gold loan",
        "asset quality",
        "fintech lending",
        "shadow bank",
    ),
    "oil_gas_energy": (
        "crude oil",
        "natural gas",
        "refinery margin",
        "oil marketing",
        "upstream oil",
        "energy price",
        "petrol diesel",
    ),
    "pharma_healthcare": (
        "pharma",
        "drug approval",
        "healthcare sector",
        "generic medicine",
        "api export",
        "clinical trial",
        "hospital chain",
        "usfda",
    ),
    "power_utilities": (
        "power demand",
        "electricity tariff",
        "thermal power",
        "power generation",
        "discom",
        "grid capacity",
        "open access power",
    ),
    "realty_reits": (
        "real estate",
        "housing sales",
        "property market",
        "reit",
        "commercial office",
        "residential demand",
        "home sales",
    ),
    "telecom": (
        "telecom sector",
        "mobile subscriber",
        "5g rollout",
        "spectrum auction",
        "tariff hike",
        "data usage",
        "broadband penetration",
    ),
    "textiles": (
        "textile export",
        "cotton yarn",
        "garment export",
        "apparel export",
        "spinning mill",
        "fabric demand",
        "textile pli",
    ),
}
_SECTOR_THEME_NEGATIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "defence": (
        "immigration",
        "deportation",
        "migrant",
        "asylum",
        "border wall",
        "undocumented",
    ),
}
_SECTOR_THEME_WORD_BOUNDARY_TERMS: dict[str, tuple[str, ...]] = {
    "defence": ("military",),
}

# P1: sector-tuned constructive thresholds consumed by signal_v2 (always-on, no flags).
_SECTOR_SIGNAL_PROFILES: dict[str, dict[str, float]] = {
    "ai": {
        "buy_nw_min": 68.0,
        "buy_intent_min": 63.0,
        "accum_intent_min": 62.0,
        "accum_nw_min": 60.0,
        "participation_intent_min": 68.0,
        "participation_nw_min": 63.0,
        "participation_vpr_min": 1.4,
        "leader_intent_min": 68.0,
        "leader_vpr_min": 1.6,
        "cmf_constructive_min": 0.0,
    },
}


def sector_signal_profile_for(sector_key: str) -> dict[str, float]:
    """Return sector-specific signal_v2 threshold overrides (empty when none)."""
    sec = str(sector_key or "").strip().lower()
    profile = _SECTOR_SIGNAL_PROFILES.get(sec)
    return dict(profile) if profile else {}


def enrich_audit_sector_signal_profile(audit: dict[str, Any], sector_key: str) -> None:
    """Attach sector signal profile + sector_key onto an audit dict in place."""
    if not isinstance(audit, dict):
        return
    sec = str(sector_key or audit.get("sector_key") or audit.get("sector") or "").strip().lower()
    if sec and not audit.get("sector_key"):
        audit["sector_key"] = sec
    if audit.get("sector_signal_profile"):
        return
    profile = sector_signal_profile_for(sec)
    if profile:
        audit["sector_signal_profile"] = profile

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
_STOCK_NEWS_FETCH_DELAY_SEC_DEFAULT = 0.75
_STOCK_NEWS_SYMBOL_DISPLAY_ALIASES: dict[str, str] = {
    "ANANTRAJ": "Anant Raj",
    "E2E": "E2E Networks",
}
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


def _stock_news_coverage_top_n() -> int:
    """Return live-fetch cap; 0 means fetch every symbol in the scan."""
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_COVERAGE_TOP_N", "")) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _stock_news_fetch_delay_seconds() -> float:
    raw = (str(os.environ.get("TITAN_STOCK_NEWS_FETCH_DELAY_SEC", "")) or "").strip()
    if not raw:
        return _STOCK_NEWS_FETCH_DELAY_SEC_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _STOCK_NEWS_FETCH_DELAY_SEC_DEFAULT


def _select_live_fetch_pairs(coverage_pairs: list[tuple[str, str]] | set[tuple[str, str]]) -> set[tuple[str, str]]:
    pairs = sorted({(str(s).strip().upper(), str(e).strip().upper()) for s, e in coverage_pairs if s and e})
    top_n = _stock_news_coverage_top_n()
    if top_n <= 0:
        return set(pairs)
    return set(pairs[:top_n])


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
    display = _STOCK_NEWS_SYMBOL_DISPLAY_ALIASES.get(sym, "").strip()
    if display and display not in out:
        out.append(display)
    return out


def _stock_news_legal_name_search_variants(legal_name: str) -> list[str]:
    name = str(legal_name or "").strip()
    if not name:
        return []
    variants = [name]
    words = name.split()
    if len(words) >= 2:
        short_words: list[str] = []
        for word in words:
            upper = word.upper()
            if upper in ("LIMITED", "LTD", "LTD.", "COMPANY", "CO", "CORP", "CORPORATION"):
                continue
            if word.isupper() and len(word) <= 4:
                short_words.append(word)
            else:
                short_words.append(word.capitalize())
        short = " ".join(short_words).strip()
        if short and short.upper() != name.upper() and short not in variants:
            variants.append(short)
    return variants


def _stock_news_query_exclusions() -> str:
    return '-recommend -"stocks to buy" -target -"stock picks" -multibagger'


def _stock_news_name_tokens(*, symbol: str, aliases: list[str]) -> list[str]:
    sym = str(symbol or "").strip().upper()
    tokens: list[str] = []
    min_tok_len = 3 if sym and len(sym) <= 4 else 4
    if sym and len(sym) >= min_tok_len:
        tokens.append(sym.lower())
    for raw in aliases:
        phrase = str(raw or "").strip()
        if phrase and phrase.upper() != sym:
            phrase_norm = _normalize_news_text(phrase)
            if phrase_norm and phrase_norm not in tokens:
                tokens.append(phrase_norm)
        for part in re.split(r"[^a-zA-Z0-9]+", phrase):
            tok = part.strip().lower()
            if len(tok) < min_tok_len or tok in _STOCK_NAME_STOPWORDS:
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


def _stock_news_relevance_score(
    *,
    symbol: str,
    aliases: list[str],
    item: dict[str, Any],
    sector_key: str = "",
) -> float:
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
    sector = str(sector_key or "").strip().lower()
    if sector:
        theme_weight = _theme_hits_for_sector(text, sector)
        if theme_weight > 0.0:
            score += min(0.10, theme_weight * 0.05)
    url = str(item.get("url") or "").lower()
    source_blob = f"{source} {url}"
    if any(fragment in source_blob for fragment in _stock_news_quality_source_fragments()):
        score += 0.15
    if any(fragment in source_blob for fragment in _STOCK_NEWS_AGGREGATOR_FRAGMENTS):
        score -= 0.10
    pos_hits = sum(1 for sig in _stock_news_positive_signals() if sig in text)
    score += min(0.30, pos_hits * 0.10)
    return round(_clamp(score, 0.0, 1.0), 4)


def _stock_news_query_is_specific(*, query: str, symbol: str, aliases: list[str]) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    sym = str(symbol or "").strip().upper()
    q_upper = q.upper()
    if sym and re.search(rf"\b{re.escape(sym)}\b", q_upper):
        return True
    for raw in aliases:
        alias = str(raw or "").strip()
        if not alias or alias.upper() == sym:
            continue
        if f'"{alias}"' in q or f'"{alias.upper()}"' in q_upper:
            return True
        for variant in _stock_news_legal_name_search_variants(alias):
            if f'"{variant}"' in q or variant.upper() in q_upper:
                return True
    return False


def _filter_stock_news_items(
    *,
    symbol: str,
    aliases: list[str],
    items: list[dict[str, Any]],
    sector_key: str = "",
    strict_identity: bool = True,
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
            if strict_identity and not _stock_news_title_identity_match(
                symbol=symbol, aliases=aliases, title=title
            ):
                if not _stock_news_summary_identity_match(symbol=symbol, aliases=aliases, summary=summary):
                    rejected.append({"title": title[:120], "reason": "identity_mismatch"})
                    continue
        relevance = _stock_news_relevance_score(
            symbol=symbol,
            aliases=aliases,
            item=item,
            sector_key=sector_key,
        )
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
    name_variants = _stock_news_legal_name_search_variants(legal_name) if legal_name else []
    primary: list[str] = []
    for variant in name_variants:
        primary.append(f'"{variant}" NSE when:7d {exclusions}')
        primary.append(f'"{variant}" when:7d bulk OR block OR stake OR results {exclusions}')
    if sym:
        primary.append(f"{sym} NSE when:7d {exclusions}")
        primary.append(f"{sym} stock India when:7d {exclusions}")
    fallback: list[str] = []
    for variant in name_variants:
        fallback.append(f'"{variant}" stock India')
        fallback.append(f'"{variant}" NSE')
    if sym:
        fallback.append(f"{sym} stock India")
        fallback.append(f"{sym} NSE")
        if sym in _STOCK_NEWS_SYMBOL_DISPLAY_ALIASES:
            display = _STOCK_NEWS_SYMBOL_DISPLAY_ALIASES[sym]
            fallback.append(f'"{display}" stock India')
            fallback.append(f'"{display}" NSE')
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


def _finalize_stock_news_batch(
    raw_items: list[dict[str, Any]],
    *,
    symbol: str,
    aliases: list[str],
    now: datetime,
    max_items: int,
    strict_identity: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
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
        symbol=symbol,
        aliases=aliases,
        items=filter_input,
        strict_identity=strict_identity,
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
    return final_items, filter_meta, filter_input


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
    primary_google_items = list(google_items)
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
    strict_identity = not _stock_news_query_is_specific(query=used_query, symbol=sym, aliases=aliases)
    final_items, filter_meta, filter_input = _finalize_stock_news_batch(
        raw_items,
        symbol=sym,
        aliases=aliases,
        now=now,
        max_items=max_items,
        strict_identity=strict_identity,
    )
    if (
        not final_items
        and primary_google_items
        and fallback_queries
        and not query_fallback_used
    ):
        nse_only = [
            item
            for item in raw_items
            if str(item.get("source") or "").strip().lower() in _STOCK_NEWS_NSE_SOURCES
        ]
        fallback_items, fb_query, fb_alias, fb_error = _fetch_google_news_rss_items(
            fallback_queries,
            timeout_seconds=timeout_seconds,
            symbol=sym,
        )
        if fallback_items:
            google_rss_found = True
            query_fallback_used = True
            raw_items = nse_only + fallback_items
            if fb_query:
                used_query = fb_query
            if fb_alias:
                used_alias = fb_alias
            last_error = ""
            strict_identity = not _stock_news_query_is_specific(
                query=used_query, symbol=sym, aliases=aliases
            )
            final_items, filter_meta, filter_input = _finalize_stock_news_batch(
                raw_items,
                symbol=sym,
                aliases=aliases,
                now=now,
                max_items=max_items,
                strict_identity=strict_identity,
            )
        elif fb_error and not last_error:
            last_error = fb_error
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


def _news_feed_rows_to_correlator_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map news_feed rows to the item shape expected by correlate_stock_news_with_macro."""
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "summary": str(row.get("summary") or "").strip(),
                "source": str(row.get("source") or "news_feed").strip() or "news_feed",
                "url": str(row.get("url") or "").strip(),
                "published_at": str(row.get("published_at") or "").strip(),
            }
        )
    return items


def resolve_stock_news_for_symbol(
    cfg: TitanConfig,
    *,
    symbol: str,
    exchange: str,
    allow_live_fetch: bool,
) -> dict[str, Any]:
    """Prefer cached news_feed rows; fall back to live fetch when allowed."""
    sym = str(symbol).strip().upper()
    ex = str(exchange).strip().upper()
    aliases = _instrument_alias_candidates(cfg, symbol=sym, exchange=ex)
    try:
        from news_store import get_recent_news_for_symbol
    except ImportError as exc:
        logger.warning("news_store unavailable for %s (%s): %s", sym, ex, exc)
        get_recent_news_for_symbol = None  # type: ignore[misc, assignment]

    if get_recent_news_for_symbol is not None:
        try:
            lookback_hours = int(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", 36))
            fetch_limit = int(os.environ.get("TITAN_NEWS_FETCH_LIMIT", 40))
            cached_rows = get_recent_news_for_symbol(
                cfg,
                sym,
                ex,
                lookback_hours=lookback_hours,
                limit=fetch_limit,
            )
            cached_items = _news_feed_rows_to_correlator_items(cached_rows)
            if cached_items:
                return {
                    "symbol": sym,
                    "exchange": ex,
                    "items": cached_items,
                    "aliases": aliases,
                    "query_used": "",
                    "alias_used": "",
                    "fallback_used": False,
                    "error": "",
                    "data_source": "news_feed_cache",
                }
        except Exception as exc:
            logger.warning("Cached news read failed for %s (%s): %s", sym, ex, exc)

    if not allow_live_fetch:
        return {
            "symbol": sym,
            "exchange": ex,
            "items": [],
            "aliases": aliases,
            "query_used": "",
            "alias_used": "",
            "fallback_used": False,
            "error": "cache_empty_live_skipped",
            "data_source": "none",
        }

    try:
        live = fetch_stock_news_for_symbol(cfg, symbol=sym, exchange=ex)
        if isinstance(live, dict):
            live_aliases = live.get("aliases")
            if not isinstance(live_aliases, list):
                live = {**live, "aliases": aliases}
            return {**live, "data_source": "google_rss_live"}
    except Exception as exc:
        return {
            "symbol": sym,
            "exchange": ex,
            "items": [],
            "aliases": aliases,
            "query_used": "",
            "alias_used": "",
            "fallback_used": False,
            "error": f"unexpected:{exc}",
            "data_source": "none",
        }
    return {
        "symbol": sym,
        "exchange": ex,
        "items": [],
        "aliases": aliases,
        "query_used": "",
        "alias_used": "",
        "fallback_used": False,
        "error": "unavailable",
        "data_source": "none",
    }


def resolve_stock_news_batch(
    cfg: TitanConfig,
    *,
    pairs: list[tuple[str, str]],
    allow_live_fetch_for: set[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    allow = allow_live_fetch_for if allow_live_fetch_for is not None else _select_live_fetch_pairs(pairs)
    delay_seconds = _stock_news_fetch_delay_seconds()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    live_fetch_count = 0
    for symbol, exchange in pairs:
        sym = str(symbol).strip().upper()
        ex = str(exchange).strip().upper()
        if not sym or ex not in ("NSE", "BSE"):
            continue
        allow_live = (sym, ex) in allow
        if allow_live and live_fetch_count > 0 and delay_seconds > 0:
            time.sleep(delay_seconds)
        out[(sym, ex)] = resolve_stock_news_for_symbol(
            cfg,
            symbol=sym,
            exchange=ex,
            allow_live_fetch=allow_live,
        )
        if allow_live:
            live_fetch_count += 1
    return out


def _stock_news_coverage_status(
    *,
    pair: tuple[str, str],
    stock_news_meta: dict[str, Any],
    resolved_keys: set[tuple[str, str]],
) -> str:
    stock_news_items = stock_news_meta.get("items")
    stock_news_items = stock_news_items if isinstance(stock_news_items, list) else []
    stock_fetch_error = str(stock_news_meta.get("error") or "").strip()
    data_source = str(stock_news_meta.get("data_source") or "").strip()
    if pair not in resolved_keys:
        return "not_covered"
    if stock_news_items:
        return "cached" if data_source == "news_feed_cache" else "fetched"
    if stock_fetch_error == "cache_empty_live_skipped":
        return "empty:cache_miss_live_skipped"
    if stock_fetch_error == "helper_unavailable":
        return "helper_unavailable"
    if stock_fetch_error:
        return f"empty:{stock_fetch_error}"
    return "empty:unknown"


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


def _theme_term_in_text(text: str, term: str, *, word_boundary: bool) -> bool:
    if not term:
        return False
    if word_boundary:
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def _theme_hits_for_sector(text: str, sector_key: str) -> float:
    sector = sector_key.strip().lower()
    negatives = _SECTOR_THEME_NEGATIVE_PATTERNS.get(sector, ())
    if negatives and any(term in text for term in negatives):
        return 0.0
    terms = _SECTOR_THEME_KEYWORDS.get(sector, ())
    if not terms:
        return 0.0
    boundary_terms = set(_SECTOR_THEME_WORD_BOUNDARY_TERMS.get(sector, ()))
    hits = sum(
        1
        for t in terms
        if _theme_term_in_text(text, t, word_boundary=t in boundary_terms)
    )
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


def build_macro_news_layers(
    snapshot: dict[str, Any],
    *,
    sector_key: str,
    allow_cross_sector_fallback: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    layers: dict[str, list[dict[str, Any]]] = {"global": [], "local": [], "market": []}
    scores = snapshot.get("sector_scores")
    score_map = scores if isinstance(scores, dict) else {}
    sector = str(sector_key or "").strip().lower()
    sector_row = score_map.get(sector) if isinstance(score_map.get(sector), dict) else {}
    drivers = sector_row.get("drivers_top") if isinstance(sector_row.get("drivers_top"), list) else []
    if not drivers and allow_cross_sector_fallback:
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
    stock_news_fetch_error: str | None = None,
) -> dict[str, Any]:
    alias_list = aliases if isinstance(aliases, list) else []
    sector = str(sector_key or "").strip().lower()
    sym = str(symbol or "").strip().upper()
    fetch_error = str(stock_news_fetch_error or "").strip()
    filtered_items, filter_meta = _filter_stock_news_items(
        symbol=symbol,
        aliases=alias_list,
        items=stock_news_items,
        sector_key=sector,
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
            relevance = _stock_news_relevance_score(
                symbol=symbol,
                aliases=alias_list,
                item=item,
                sector_key=sector,
            )
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
    macro_layers = build_macro_news_layers(
        snapshot,
        sector_key=sector,
        allow_cross_sector_fallback=False,
    )
    stock_news_no_relevant = bool(stock_news_items) and not stock_rows
    stock_news_all_filtered = not stock_news_items and fetch_error == "all_filtered"
    suppress_macro_driver = stock_news_no_relevant or stock_news_all_filtered
    evidence_layers = {
        "global": [] if suppress_macro_driver else macro_layers.get("global", [])[:2],
        "local": [] if suppress_macro_driver else macro_layers.get("local", [])[:2],
        "market": [] if suppress_macro_driver else macro_layers.get("market", [])[:2],
        "stock": stock_rows[:2],
    }
    stock_driver = stock_rows[0] if stock_rows else {}
    if stock_driver:
        driver = f"{stock_driver.get('headline')} ({stock_driver.get('source')})"
        fallback_label = ""
    elif suppress_macro_driver:
        driver = f"No relevant news for {sym or 'symbol'}"
        if stock_news_no_relevant:
            fallback_label = "stock_news_no_relevant_items"
        else:
            fallback_label = "stock_news_all_filtered"
    else:
        macro_driver = (macro_layers.get("market") or macro_layers.get("local") or macro_layers.get("global") or [{}])[0]
        driver_headline = str(macro_driver.get("headline") or "No recent market driver available").strip()
        driver_source = str(macro_driver.get("source") or "snapshot_unavailable").strip()
        driver = f"{driver_headline} ({driver_source})"
        if driver_headline == "No recent market driver available":
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



def _sector_relative_weighted_score(
    *,
    sector_pctile_return_1m: float,
    sector_pctile_return_3m: float,
    sector_pctile_rel_strength: float,
    sector_pctile_intent: float,
    sector_pctile_next_week: float,
    weights: tuple[float, ...],
) -> float:
    inputs = (
        sector_pctile_return_1m,
        sector_pctile_return_3m,
        sector_pctile_rel_strength,
        sector_pctile_intent,
        sector_pctile_next_week,
    )
    total = 0.0
    for w, v in zip(weights, inputs):
        pct = 50.0 if math.isnan(_safe_float(v)) else _clamp(float(v), 0.0, 100.0)
        total += w * pct
    return round(_clamp(total, 0.0, 100.0), 4)


def compute_sector_relative_rank_score(
    *,
    sector_pctile_return_1m: float,
    sector_pctile_return_3m: float,
    sector_pctile_rel_strength: float,
    sector_pctile_intent: float,
    sector_pctile_next_week: float,
) -> float:
    """V2 review sector-relative rank score (0-100) with intent/next-week emphasis."""
    return _sector_relative_weighted_score(
        sector_pctile_return_1m=sector_pctile_return_1m,
        sector_pctile_return_3m=sector_pctile_return_3m,
        sector_pctile_rel_strength=sector_pctile_rel_strength,
        sector_pctile_intent=sector_pctile_intent,
        sector_pctile_next_week=sector_pctile_next_week,
        weights=_SECTOR_RELATIVE_RANK_WEIGHTS,
    )


def compute_sector_relative_rank_components(
    *,
    sector_pctile_return_1m: float,
    sector_pctile_return_3m: float,
    sector_pctile_rel_strength: float,
    sector_pctile_intent: float,
    sector_pctile_next_week: float,
) -> dict[str, Any]:
    """Expose weighted sector-relative rank terms for ranking meta transparency."""
    raw_inputs = (
        sector_pctile_return_1m,
        sector_pctile_return_3m,
        sector_pctile_rel_strength,
        sector_pctile_intent,
        sector_pctile_next_week,
    )
    components: dict[str, dict[str, float]] = {}
    score = 0.0
    for key, weight, raw in zip(_SECTOR_RELATIVE_RANK_FEATURE_KEYS, _SECTOR_RELATIVE_RANK_WEIGHTS, raw_inputs):
        pctile = 50.0 if math.isnan(_safe_float(raw)) else _clamp(float(raw), 0.0, 100.0)
        weighted = weight * pctile
        score += weighted
        components[key] = {
            "pctile": round(pctile, 4),
            "weight": weight,
            "weighted": round(weighted, 4),
        }
    return {
        "score": round(_clamp(score, 0.0, 100.0), 4),
        "weights": _SECTOR_RELATIVE_RANK_WEIGHTS,
        "components": components,
    }


def compute_sector_relative_momentum_score(
    *,
    sector_pctile_return_1m: float,
    sector_pctile_return_3m: float,
    sector_pctile_rel_strength: float,
    sector_pctile_intent: float,
    sector_pctile_next_week: float,
) -> float:
    """2de00ac sector-relative momentum score (distinct weights from rank score)."""
    return _sector_relative_weighted_score(
        sector_pctile_return_1m=sector_pctile_return_1m,
        sector_pctile_return_3m=sector_pctile_return_3m,
        sector_pctile_rel_strength=sector_pctile_rel_strength,
        sector_pctile_intent=sector_pctile_intent,
        sector_pctile_next_week=sector_pctile_next_week,
        weights=_SECTOR_RELATIVE_MOMENTUM_WEIGHTS,
    )


def _cohort_return_percentiles(pending: list[dict[str, Any]]) -> None:
    """Assign cross-sectional return percentiles; NaN returns -> sector median (50)."""
    ret_1w_vals = [float(p["ret_1w"]) for p in pending if not math.isnan(_safe_float(p.get("ret_1w")))]
    ret_1m_vals = [float(p["ret_1m"]) for p in pending if not math.isnan(_safe_float(p.get("ret_1m")))]
    ret_3m_vals = [float(p["ret_3m"]) for p in pending if not math.isnan(_safe_float(p.get("ret_3m")))]
    rel_vals = [float(p["rel_strength"]) for p in pending if not math.isnan(_safe_float(p.get("rel_strength")))]
    for p in pending:
        r1w = _safe_float(p.get("ret_1w"))
        r1m = _safe_float(p.get("ret_1m"))
        r3m = _safe_float(p.get("ret_3m"))
        rel = _safe_float(p.get("rel_strength"))
        p["percentile_1w"] = 50.0 if math.isnan(r1w) else percentile_rank_0_100(ret_1w_vals, r1w)
        p["percentile_1m"] = 50.0 if math.isnan(r1m) else percentile_rank_0_100(ret_1m_vals, r1m)
        p["percentile_3m"] = 50.0 if math.isnan(r3m) else percentile_rank_0_100(ret_3m_vals, r3m)
        p["percentile_rel_strength"] = 50.0 if math.isnan(rel) else percentile_rank_0_100(rel_vals, rel)
        if math.isnan(_safe_float(p.get("percentile_intent"))):
            p["percentile_intent"] = 50.0
        if math.isnan(_safe_float(p.get("percentile_next_week"))):
            p["percentile_next_week"] = 50.0


def _return_pct(series: pd.Series, periods_back: int) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= periods_back:
        return float("nan")
    prev = float(s.iloc[-(periods_back + 1)])
    last = float(s.iloc[-1])
    if prev == 0.0:
        return float("nan")
    return ((last / prev) - 1.0) * 100.0


def _env_float(name: str, default: float) -> float:
    raw = (str(os.environ.get(name, "")) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _overextension_enabled() -> bool:
    raw = (str(os.environ.get("TITAN_OVEREXT_ENABLED", "")) or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


# Overextension penalty defaults (Fix A). Smooth ramps, not hard cliffs. Tunable via
# TITAN_OVEREXT_* env knobs. Defaults are calibrated so volatility-stretched and/or
# climactically-run winners (e.g. ABB, GREAVESCOT, EICHERMOT) are materially demoted
# while orderly risers (e.g. GARFIBRES, MAHABANK) are left almost untouched.
_OVEREXT_STRETCH_DEADBAND = 3.0  # ATR-normalized EMA200 stretch where penalty starts
_OVEREXT_STRETCH_FULL = 7.0      # stretch at which the stretch channel is maxed
_OVEREXT_STRETCH_WEIGHT = 9.0    # max points removed by the stretch channel
_OVEREXT_RUN_DEADBAND_PCT = 6.0  # 1-week return where the run channel starts
_OVEREXT_RUN_FULL_PCT = 12.0     # 1-week return at which the run channel is maxed
_OVEREXT_RUN_WEIGHT = 6.0        # max points removed by the run channel (pre-amp)
_OVEREXT_ABSORPTION_AMP = 0.25   # volume-confirmation amplifier on the run channel
_OVEREXT_PENALTY_CAP = 18.0      # absolute cap on total penalty
# Run-gate on the stretch channel: a name that is ATR-stretched but has not actually
# run up recently (flat/falling 1w & 1m) is a high-beta name far from a distant EMA200,
# not a fresh blow-off, so the stretch penalty is scaled down by recent run context.
_OVEREXT_RUN_GATE_ZERO_PCT = 0.0  # recent run (max of 1w/1m) at which the gate is fully off
_OVEREXT_RUN_GATE_FULL_PCT = 4.0  # recent run at which the gate is fully on


def _ramp(value: float, zero_at: float, full_at: float, full_points: float) -> float:
    """Linear 0->full_points ramp between zero_at and full_at (clamped); NaN -> 0."""
    if math.isnan(value) or zero_at == full_at:
        return 0.0
    frac = (value - zero_at) / (full_at - zero_at)
    return _clamp(frac, 0.0, 1.0) * full_points


# Fix A refinement (STEP 2a): gate the penalty behind momentum/regime confirmation so it
# stops demoting genuine continuing winners (sustained strong monthly trend + positive
# week, e.g. E2E/IDEAFORGE) while still biting stretched names that have stalled (flat/
# weak monthly trend, e.g. ABB/HINDPETRO/EICHERMOT) or sit in a hostile regime.
_OVEREXT_CONFIRM_TREND_WEAK_PCT = 6.0    # 1m return where continuation credit starts
_OVEREXT_CONFIRM_TREND_FULL_PCT = 18.0   # 1m return where continuation credit is maxed
_OVEREXT_CONFIRM_FLOOR = 0.2             # min penalty multiplier for a confirmed winner
_OVEREXT_CONFIRM_WEEKLY_FLOOR_PCT = -3.0   # mild weekly pullback still counts as continuation


def _overextension_confirm_mode() -> str:
    raw = (str(os.environ.get("TITAN_OVEREXT_CONFIRM_MODE", "")) or "").strip().lower()
    return raw if raw in ("off", "momentum", "both") else "momentum"


def _overextension_confirmation(ret_1w: float, ret_1m: float, regime_hostile: bool) -> tuple[float, str]:
    """Penalty multiplier in [floor, 1.0].

    A name with a sustained monthly uptrend AND positive weekly follow-through is a
    genuine continuing winner -> multiplier shrinks toward ``floor`` (penalty suppressed).
    A hostile regime forces the full penalty. NaN momentum inputs -> 1.0 (today's blunt
    behaviour), so missing data degrades to the pre-refinement penalty.
    """
    mode = _overextension_confirm_mode()
    if mode == "off":
        return 1.0, "off"
    if regime_hostile and mode == "both":
        return 1.0, "regime_hostile"
    if math.isnan(ret_1m) or math.isnan(ret_1w):
        return 1.0, "nan_inputs"
    weak = _env_float("TITAN_OVEREXT_CONFIRM_TREND_WEAK_PCT", _OVEREXT_CONFIRM_TREND_WEAK_PCT)
    full = _env_float("TITAN_OVEREXT_CONFIRM_TREND_FULL_PCT", _OVEREXT_CONFIRM_TREND_FULL_PCT)
    floor = _env_float("TITAN_OVEREXT_CONFIRM_FLOOR", _OVEREXT_CONFIRM_FLOOR)
    weekly_floor = _env_float(
        "TITAN_OVEREXT_CONFIRM_WEEKLY_FLOOR_PCT", _OVEREXT_CONFIRM_WEEKLY_FLOOR_PCT
    )
    cont = _ramp(ret_1m, weak, full, 1.0)
    # Mild weekly pullbacks within a strong monthly uptrend still get continuation credit
    # (e.g. TRENT: slight 1w dip while 1m trend remains constructive).
    if ret_1w <= weekly_floor or (ret_1w <= 0.0 and ret_1m < weak):
        cont = 0.0
    mult = 1.0 - (1.0 - _clamp(floor, 0.0, 1.0)) * cont
    return _clamp(mult, floor, 1.0), f"cont={round(cont, 3)}"


def _overextension_penalty(
    *,
    ret_1w: float,
    ret_1m: float,
    absorption: float,
    stretch: float = float("nan"),
    ema_dist: float = float("nan"),
    regime_hostile: bool = False,
) -> dict[str, Any]:
    """Smooth overextension penalty for the winner rank_score (Fix A).

    Two ramped channels, summed and capped:
      * ATR-normalized stretch (ema_200_distance_pct / atr_14_pct) above a deadband
        -- a high distance that is *small* relative to volatility is not penalized.
      * 1-week run, amplified when the run is backed by high volume participation /
        absorption (a climactic, volume-confirmed blow-off mean-reverts more often).
    ``ema_dist`` is accepted for explainability/future use; scoring uses ``stretch``.
    Returns a positive penalty plus a component breakdown for score_breakdown.
    """
    if not _overextension_enabled():
        return {"penalty": 0.0, "components": {}, "enabled": False}
    s_dead = _env_float("TITAN_OVEREXT_STRETCH_DEADBAND", _OVEREXT_STRETCH_DEADBAND)
    s_full = _env_float("TITAN_OVEREXT_STRETCH_FULL", _OVEREXT_STRETCH_FULL)
    s_w = _env_float("TITAN_OVEREXT_STRETCH_WEIGHT", _OVEREXT_STRETCH_WEIGHT)
    r_dead = _env_float("TITAN_OVEREXT_RUN_DEADBAND_PCT", _OVEREXT_RUN_DEADBAND_PCT)
    r_full = _env_float("TITAN_OVEREXT_RUN_FULL_PCT", _OVEREXT_RUN_FULL_PCT)
    r_w = _env_float("TITAN_OVEREXT_RUN_WEIGHT", _OVEREXT_RUN_WEIGHT)
    abs_amp = _env_float("TITAN_OVEREXT_ABSORPTION_AMP", _OVEREXT_ABSORPTION_AMP)
    cap = _env_float("TITAN_OVEREXT_PENALTY_CAP", _OVEREXT_PENALTY_CAP)
    g_zero = _env_float("TITAN_OVEREXT_RUN_GATE_ZERO_PCT", _OVEREXT_RUN_GATE_ZERO_PCT)
    g_full = _env_float("TITAN_OVEREXT_RUN_GATE_FULL_PCT", _OVEREXT_RUN_GATE_FULL_PCT)

    # Gate the stretch channel by recent run so a stretched-but-not-running name (flat or
    # falling 1w & 1m) is not treated as an overbought blow-off.
    run_ctx = max(
        -1e9 if math.isnan(ret_1w) else ret_1w,
        -1e9 if math.isnan(ret_1m) else ret_1m,
    )
    run_gate = 1.0 if run_ctx <= -1e8 else _ramp(run_ctx, g_zero, g_full, 1.0)
    stretch_pen = _ramp(stretch, s_dead, s_full, s_w) * run_gate
    run_base = _ramp(ret_1w, r_dead, r_full, r_w)
    amp = 1.0
    if not math.isnan(absorption):
        amp = 1.0 + abs_amp * _clamp(absorption - 1.0, 0.0, 2.0)
    run_pen = run_base * amp
    raw_penalty = stretch_pen + run_pen
    # STEP 2a: gate the penalty behind momentum/regime confirmation.
    confirm_mult, confirm_reason = _overextension_confirmation(ret_1w, ret_1m, regime_hostile)
    penalty = _clamp(raw_penalty * confirm_mult, 0.0, cap)
    return {
        "penalty": round(penalty, 4),
        "components": {
            "stretch": _round_or_none(stretch),
            "ema_200_distance_pct": _round_or_none(ema_dist),
            "stretch_penalty": round(stretch_pen, 4),
            "run_gate": round(run_gate, 4),
            "run_1w_penalty": round(run_pen, 4),
            "absorption_amp": round(amp, 4),
            "raw_penalty": round(raw_penalty, 4),
            "confirm_mult": round(confirm_mult, 4),
            "confirm_reason": confirm_reason,
        },
        "enabled": True,
    }


def _stretch_inputs_from_df(df: pd.DataFrame, series: pd.Series) -> tuple[float, float]:
    """Return (ema_200_distance_pct, ema200_stretch_atr) from a price frame.

    NaN-safe: returns NaNs when history is too short for EMA200 or OHLC for ATR is
    unavailable (the common case for the ranking module's short live fetch).
    """
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 1:
        return float("nan"), float("nan")
    try:
        from titan_engine import calculate_atr, calculate_ema
    except Exception:  # noqa: BLE001
        return float("nan"), float("nan")
    close_last = float(s.iloc[-1])
    ema_200 = calculate_ema(s, span=200)
    if math.isnan(ema_200) or ema_200 == 0.0 or math.isnan(close_last):
        return float("nan"), float("nan")
    ema_dist = ((close_last / ema_200) - 1.0) * 100.0
    atr_14 = calculate_atr(df, window=14)
    if math.isnan(atr_14) or close_last == 0.0:
        return ema_dist, float("nan")
    atr_pct = (atr_14 / close_last) * 100.0
    if math.isnan(atr_pct) or atr_pct == 0.0:
        return ema_dist, float("nan")
    return ema_dist, ema_dist / atr_pct


# P0-2 (STEP 3a): the legacy absorption bonus ``(absorption-1)*8`` is uncapped and
# sign-blind, so a high-volume DOWN day (distribution) produces a large POSITIVE score
# (e.g. BDL crowned #1 off an 8.85x down day). This caps the contribution and zeroes the
# bonus on a down session. Shadow-first: default mode keeps the legacy value in the
# published score and only records the would-be gated value.
_ABS_TERM_MULT = 8.0
_ABS_TERM_CAP = 12.0
_ABS_DOWN_DAY_MULT = 0.0   # multiplier applied to a POSITIVE absorption bonus on a down day


def _absorption_term(absorption: float, session_move: float = float("nan")) -> dict[str, Any]:
    """Return the absorption score contribution under the active mode.

    mode (``TITAN_ABS_TERM_MODE``): off/shadow -> legacy uncapped value (default, no-op);
    damp -> half-way to the gated value; enforce -> capped + down-day sign-gated value.
    NaN absorption -> 0.0.
    """
    raw_mode = (str(os.environ.get("TITAN_ABS_TERM_MODE", "")) or "").strip().lower()
    mode = raw_mode if raw_mode in ("off", "shadow", "damp", "enforce") else "shadow"
    mult = _env_float("TITAN_ABS_TERM_MULT", _ABS_TERM_MULT)
    cap = _env_float("TITAN_ABS_TERM_CAP", _ABS_TERM_CAP)
    down_mult = _env_float("TITAN_ABS_DOWN_DAY_MULT", _ABS_DOWN_DAY_MULT)
    if math.isnan(absorption):
        return {"value": 0.0, "legacy": 0.0, "gated": 0.0, "mode": mode,
                "down_day": False, "capped": False}
    legacy = (absorption - 1.0) * mult
    gated = legacy
    down_day = (not math.isnan(session_move)) and session_move < 0.0
    if down_day and gated > 0.0:
        gated *= down_mult  # absorption on a down day is distribution, not accumulation
    capped = False
    if gated > cap:
        gated, capped = cap, True
    elif gated < -cap:
        gated, capped = -cap, True
    if mode in ("off", "shadow"):
        applied = legacy
    elif mode == "damp":
        applied = 0.5 * legacy + 0.5 * gated
    else:
        applied = gated
    return {"value": round(applied, 4), "legacy": round(legacy, 4), "gated": round(gated, 4),
            "mode": mode, "down_day": down_day, "capped": capped}


def _score_from_features(
    *,
    bucket: str,
    ret_1w: float,
    ret_1m: float,
    absorption: float,
    stretch: float = float("nan"),
    ema_dist: float = float("nan"),
    regime_hostile: bool = False,
    session_move: float = float("nan"),
    percentile_1w: float = 50.0,
    percentile_1m: float = 50.0,
    sector_relative_rank_score: float | None = None,
    sector_pctile_return_1m: float | None = None,
    sector_pctile_return_3m: float | None = None,
    sector_pctile_rel_strength: float | None = None,
    sector_pctile_intent: float | None = None,
    sector_pctile_next_week: float | None = None,
) -> float:
    ref_1w = _env_float("TITAN_RANK_PCTILE_1W_REF", _PCTILE_1W_REF_RETURN)
    ref_1m = _env_float("TITAN_RANK_PCTILE_1M_REF", _PCTILE_1M_REF_RETURN)
    absorption_term = _absorption_term(absorption, session_move)["value"]
    ref_total = _env_float("TITAN_SRM_REF_POINTS", ref_1w + ref_1m)
    srr = sector_relative_rank_score
    if srr is None:
        srr = compute_sector_relative_rank_score(
            sector_pctile_return_1m=sector_pctile_return_1m
            if sector_pctile_return_1m is not None
            else percentile_1m,
            sector_pctile_return_3m=sector_pctile_return_3m
            if sector_pctile_return_3m is not None
            else 50.0,
            sector_pctile_rel_strength=sector_pctile_rel_strength
            if sector_pctile_rel_strength is not None
            else 50.0,
            sector_pctile_intent=sector_pctile_intent if sector_pctile_intent is not None else 50.0,
            sector_pctile_next_week=sector_pctile_next_week
            if sector_pctile_next_week is not None
            else 50.0,
        )
    momentum_term = (float(srr) / 100.0) * ref_total
    score = _cap_bias(bucket) + momentum_term + absorption_term
    # Fix A: down-rank statistically extended winners (smooth, env-tunable; STEP 2a
    # gates the penalty behind momentum/regime confirmation).
    penalty = _overextension_penalty(
        ret_1w=ret_1w,
        ret_1m=ret_1m,
        absorption=absorption,
        stretch=stretch,
        ema_dist=ema_dist,
        regime_hostile=regime_hostile,
    )["penalty"]
    return round(score - penalty, 4)


# ---------------------------------------------------------------------------
# Shadow-mode buy-suppression gates (rollout policy: shadow -> damp -> skip).
# Every gate computes a would-be decision and a score multiplier / withhold flag.
# In the default ``shadow`` mode the multiplier is 1.0 and withhold is False, so the
# published rank_score / shortlist is unchanged; the decision is only recorded under
# meta["shadow_gates"]. ``damp`` applies a half-size multiplier; ``skip`` withholds
# the name from the priority shortlist. All inputs are NaN-safe: missing data -> no-op.
# ---------------------------------------------------------------------------

_GATE_MODES = ("off", "shadow", "damp", "skip")


def _gates_default_enforce_active() -> bool:
    raw = (str(os.environ.get("TITAN_GATES_DEFAULT_ENFORCE", "")) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _gate_mode(env_name: str, default: str = "shadow") -> str:
    raw = (str(os.environ.get(env_name, "")) or "").strip().lower()
    if raw in _GATE_MODES:
        return raw
    if default == "shadow" and _gates_default_enforce_active():
        return "damp"
    return default


def _gate_effect(mode: str, triggered: bool, damp_mult: float) -> tuple[float, bool]:
    """Translate (mode, triggered) into a (score_multiplier, withhold) effect.

    shadow/off never change the published value. damp halves (env-tunable). skip
    withholds from the shortlist (and also damps the score so any downstream rank use
    reflects the decision).
    """
    if not triggered or mode in ("off", "shadow"):
        return 1.0, False
    if mode == "damp":
        return _clamp(damp_mult, 0.0, 1.0), False
    if mode == "skip":
        return _clamp(damp_mult, 0.0, 1.0), True
    return 1.0, False


# ---- STEP 1: sector-regime gate (keyed off sector_daily_rollup) ----
_REGIME_BREADTH_FLOOR = 40.0       # breadth_above_ema200_pct below this is hostile
_REGIME_BREADTH_FALL_PTS = 12.0    # breadth drop over the lookback that counts as falling
_REGIME_INTENT_FALL_PTS = 6.0      # avg_effective_intent_score drop that counts as falling
_REGIME_LOOKBACK = 3               # rollup sessions used for the slope read
_REGIME_DAMP_MULT = 0.5


def _regime_gate_decision(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide whether a sector regime is hostile from recent rollup rows.

    ``series`` is a list of rollup dicts (any order) carrying ``trade_date``,
    ``breadth_above_ema200_pct`` and ``avg_effective_intent_score``. Combines a breadth
    floor with breadth/intent slope so a high-breadth-but-deteriorating-intent sector
    (e.g. defence) is still caught (deep-scan P1-3). NaN-safe: empty/short -> not hostile.
    """
    mode = _gate_mode("TITAN_REGIME_GATE_MODE")
    floor = _env_float("TITAN_REGIME_BREADTH_FLOOR", _REGIME_BREADTH_FLOOR)
    breadth_fall = _env_float("TITAN_REGIME_BREADTH_FALL_PTS", _REGIME_BREADTH_FALL_PTS)
    intent_fall = _env_float("TITAN_REGIME_INTENT_FALL_PTS", _REGIME_INTENT_FALL_PTS)
    lookback = max(2, int(_env_float("TITAN_REGIME_LOOKBACK", float(_REGIME_LOOKBACK))))
    damp = _env_float("TITAN_REGIME_GATE_DAMP_MULT", _REGIME_DAMP_MULT)

    rows = [r for r in series if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("trade_date") or ""))
    window = rows[-lookback:]
    breadth_vals = [_safe_float(r.get("breadth_above_ema200_pct")) for r in window]
    intent_vals = [_safe_float(r.get("avg_effective_intent_score")) for r in window]
    breadth_now = next((v for v in reversed(breadth_vals) if not math.isnan(v)), float("nan"))
    breadth_first = next((v for v in breadth_vals if not math.isnan(v)), float("nan"))
    intent_now = next((v for v in reversed(intent_vals) if not math.isnan(v)), float("nan"))
    intent_first = next((v for v in intent_vals if not math.isnan(v)), float("nan"))

    reasons: list[str] = []
    below_floor = (not math.isnan(breadth_now)) and breadth_now < floor
    breadth_drop = (
        breadth_first - breadth_now
        if not (math.isnan(breadth_now) or math.isnan(breadth_first))
        else float("nan")
    )
    intent_drop = (
        intent_first - intent_now
        if not (math.isnan(intent_now) or math.isnan(intent_first))
        else float("nan")
    )
    breadth_falling = (not math.isnan(breadth_drop)) and breadth_drop >= breadth_fall
    intent_falling = (not math.isnan(intent_drop)) and intent_drop >= intent_fall
    if below_floor:
        reasons.append(f"breadth {breadth_now:.0f}% < floor {floor:.0f}%")
    if breadth_falling:
        reasons.append(f"breadth falling {breadth_first:.0f}->{breadth_now:.0f}%")
    if intent_falling:
        reasons.append(f"intent falling {intent_first:.1f}->{intent_now:.1f}")
    triggered = below_floor or breadth_falling or intent_falling
    mult, withhold = _gate_effect(mode, triggered, damp)
    return {
        "gate": "sector_regime",
        "mode": mode,
        "triggered": triggered,
        "would": "withhold/damp new buys" if triggered else "allow",
        "reasons": reasons,
        "breadth_now": _round_or_none(breadth_now, 2),
        "breadth_prev": _round_or_none(breadth_first, 2),
        "intent_now": _round_or_none(intent_now, 2),
        "intent_prev": _round_or_none(intent_first, 2),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


_LIVE_REGIME_HOSTILE = frozenset({"risk_off", "rolling_over"})
# Phase 1 subset: map Titan sectors to subscribed index codes (see live_stream_consumer).
_SECTOR_LIVE_INDEX: dict[str, str] = {
    "banks_psu": "NIFTY BANK",
    "banks_private": "NIFTY BANK",
}


def _live_regime_read_enabled() -> bool:
    raw = (str(os.environ.get("TITAN_LIVE_REGIME_READ", "")) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _sector_benchmark_index_code(sector_key: str) -> str:
    sec = str(sector_key or "").strip().lower()
    return _SECTOR_LIVE_INDEX.get(sec, "NIFTY")


def _fetch_latest_live_regime_snapshot(client: Any, *, index_code: str) -> dict[str, Any] | None:
    """Best-effort read of the newest live_regime_snapshots row for an index."""
    code = str(index_code or "").strip()
    if not code:
        return None
    try:
        res = (
            client.table("live_regime_snapshots")
            .select(
                "snapshot_ts,index_code,last,pct_vs_prev_close,pct_vs_open,slope_proxy,regime_state,source"
            )
            .eq("index_code", code)
            .order("snapshot_ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = list(getattr(res, "data", None) or [])
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 - read-only shadow hook; degrade to EOD-only
        logger.info("live_regime_snapshots read failed index=%s: %s", code, exc)
        return None


def _merge_regime_with_live_snapshot(
    eod: dict[str, Any], live: dict[str, Any] | None, *, sector_key: str
) -> dict[str, Any]:
    """Attach live index regime to the EOD rollup gate record (shadow-only).

    EOD ``triggered`` / multiplier / withhold are unchanged; live fields are for
    logging and forward-return measurement (P1-d). NaN-safe: missing live -> EOD only.
    """
    _ = sector_key
    if not live:
        return eod
    out = dict(eod)
    regime_state = str(live.get("regime_state") or "").strip().lower()
    live_hostile = regime_state in _LIVE_REGIME_HOSTILE
    out.update(
        {
            "live_index_code": live.get("index_code"),
            "live_snapshot_ts": live.get("snapshot_ts"),
            "live_regime_state": regime_state or None,
            "live_pct_vs_prev_close": _round_or_none(_safe_float(live.get("pct_vs_prev_close")), 3),
            "live_pct_vs_open": _round_or_none(_safe_float(live.get("pct_vs_open")), 3),
            "live_would_trigger": live_hostile,
            "live_shadow_mode": True,
        }
    )
    if live_hostile:
        reasons = list(out.get("reasons") or [])
        reasons.append(f"live index {live.get('index_code')} regime={regime_state} (shadow)")
        out["reasons"] = reasons
    return out


def _fetch_sector_regime(client: Any, *, sector_key: str, as_of_date: str | None = None) -> dict[str, Any]:
    """Read recent sector_daily_rollup rows and return the regime-gate decision.

    Read-only and best-effort: any failure or missing table -> empty series -> not
    hostile (NaN-safe no-op). When ``TITAN_LIVE_REGIME_READ=1``, also reads the
    latest ``live_regime_snapshots`` row for the sector's benchmark index and merges
    shadow fields without changing the EOD gate effect.
    """
    sec = str(sector_key or "").strip().lower()
    lookback = max(2, int(_env_float("TITAN_REGIME_LOOKBACK", float(_REGIME_LOOKBACK))))
    try:
        q = (
            client.table("sector_daily_rollup")
            .select("trade_date,breadth_above_ema200_pct,avg_effective_intent_score")
            .eq("sector", sec)
        )
        if as_of_date:
            q = q.lte("trade_date", as_of_date)
        res = q.order("trade_date", desc=True).limit(max(lookback, 4)).execute()
        rows = list(getattr(res, "data", None) or [])
    except Exception as exc:  # noqa: BLE001 - read-only context; degrade to no-op
        logger.info("regime rollup read failed for sector=%s: %s", sec, exc)
        rows = []
    decision = _regime_gate_decision(rows)
    if not _live_regime_read_enabled():
        return decision
    index_code = _sector_benchmark_index_code(sec)
    live = _fetch_latest_live_regime_snapshot(client, index_code=index_code)
    return _merge_regime_with_live_snapshot(decision, live, sector_key=sec)


def _combine_gate_effects(records: list[dict[str, Any]]) -> tuple[float, bool]:
    """Multiply the score multipliers and OR the withhold flags across gate records."""
    mult = 1.0
    withhold = False
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            mult *= float(r.get("score_multiplier", 1.0) or 1.0)
        except (TypeError, ValueError):
            pass
        withhold = withhold or bool(r.get("withhold"))
    return _clamp(mult, 0.0, 1.0), withhold


def _fetch_eod_gate_context(
    client: Any, *, symbols: list[str], as_of_date: str | None = None
) -> dict[str, Any]:
    """Bulk read the new EOD feed tables for the gate set (read-only, best-effort).

    Returns a context consumed by the per-symbol gates. Any failure / missing table /
    missing column degrades to an empty slice so the gates become NaN-safe no-ops.
    Populated by STEP 4b (delivery) / 4c (ban) / 4d (futures + institutional).
    """
    syms = sorted({str(s).strip().upper() for s in symbols if s})
    query_syms = expand_symbols_with_aliases(syms)
    ctx: dict[str, Any] = {
        "delivery": {}, "ban": set(), "futures": {}, "institutional": {},
        "calendar": {}, "as_of_date": as_of_date,
    }
    if not syms:
        return ctx
    # --- 4b: trailing delivery% / volume (delivery_daily) ---
    try:
        q = client.table("delivery_daily").select(
            "trade_date,symbol,deliv_per,deliv_qty,ttl_traded_qty"
        ).in_("symbol", query_syms)
        if as_of_date:
            q = q.lte("trade_date", as_of_date)
        res = q.order("trade_date", desc=True).limit(2000).execute()
        for r in list(getattr(res, "data", None) or []):
            ctx["delivery"].setdefault(str(r.get("symbol")).upper(), []).append(r)
    except Exception as exc:  # noqa: BLE001
        logger.info("delivery_daily read failed: %s", exc)
    # --- 4c: F&O ban list (fno_ban_daily) for the effective/as-of date ---
    try:
        q = client.table("fno_ban_daily").select("trade_date,symbol")
        if as_of_date:
            q = q.lte("trade_date", as_of_date)
        res = q.order("trade_date", desc=True).limit(1000).execute()
        ban_rows = list(getattr(res, "data", None) or [])
        latest_ban = ban_rows[0]["trade_date"] if ban_rows else None
        ctx["ban"] = {
            str(r.get("symbol")).upper() for r in ban_rows if r.get("trade_date") == latest_ban
        }
        ctx["ban_date"] = latest_ban
    except Exception as exc:  # noqa: BLE001
        logger.info("fno_ban_daily read failed: %s", exc)
    # --- 4d: futures OI / underlying close (futures_daily) ---
    try:
        q = client.table("futures_daily").select(
            "trade_date,symbol,open_interest,change_in_oi,underlying_close"
        ).in_("symbol", query_syms)
        if as_of_date:
            q = q.lte("trade_date", as_of_date)
        res = q.order("trade_date", desc=True).limit(2000).execute()
        for r in list(getattr(res, "data", None) or []):
            ctx["futures"].setdefault(str(r.get("symbol")).upper(), []).append(r)
    except Exception as exc:  # noqa: BLE001
        logger.info("futures_daily read failed: %s", exc)
    # --- 4d: market-level institutional flow (institutional_flow) ---
    try:
        q = client.table("institutional_flow").select(
            "as_of_date,segment,fii_net_crs,dii_net_crs"
        ).eq("segment", "cash")
        if as_of_date:
            q = q.lte("as_of_date", as_of_date)
        res = q.order("as_of_date", desc=True).limit(5).execute()
        inst_rows = list(getattr(res, "data", None) or [])
        if inst_rows:
            ctx["institutional"] = inst_rows[0]
    except Exception as exc:  # noqa: BLE001
        logger.info("institutional_flow read failed: %s", exc)
    # --- 3b: latest signal_v2 label + risk_net per symbol (symbol_daily_features) ---
    try:
        q = client.table("symbol_daily_features").select(
            "trade_date,symbol,action_signal,signal_reason_trace,effective_intent_score,next_week_score"
        ).in_("symbol", query_syms)
        if as_of_date:
            q = q.lte("trade_date", as_of_date)
        res = q.order("trade_date", desc=True).limit(4000).execute()
        labels: dict[str, str] = {}
        risk_net_map: dict[str, float] = {}
        label_dates: dict[str, str] = {}
        intent_map: dict[str, float] = {}
        next_week_map: dict[str, float] = {}
        for r in list(getattr(res, "data", None) or []):
            sym = str(r.get("symbol")).upper()
            if sym not in labels and r.get("action_signal"):
                labels[sym] = str(r.get("action_signal")).strip().lower()
                label_dates[sym] = str(r.get("trade_date") or "").strip() or None
            if sym not in risk_net_map:
                rn = _parse_v2_risk_net(r.get("signal_reason_trace"))
                if not math.isnan(rn):
                    risk_net_map[sym] = rn
            if sym not in intent_map:
                iv = _safe_float(r.get("effective_intent_score"))
                if not math.isnan(iv):
                    intent_map[sym] = iv
            if sym not in next_week_map:
                nw = _safe_float(r.get("next_week_score"))
                if not math.isnan(nw):
                    next_week_map[sym] = nw
        ctx["v2_labels"] = labels
        ctx["v2_risk_net"] = risk_net_map
        ctx["v2_label_dates"] = label_dates
        ctx["intent_scores"] = intent_map
        ctx["next_week_scores"] = next_week_map
    except Exception as exc:  # noqa: BLE001
        logger.info("symbol_daily_features label read failed: %s", exc)
    # --- Phase 3: corporate-actions / results calendar (corporate_actions_calendar) ---
    try:
        anchor = as_of_date or datetime.now(IST).date().isoformat()
        lookahead = max(0, int(_env_float("TITAN_CALENDAR_LOOKAHEAD_DAYS", 5.0)))
        end_date = (date.fromisoformat(anchor) + timedelta(days=lookahead)).isoformat()
        q = client.table("corporate_actions_calendar").select(
            "symbol,ex_date,purpose"
        ).in_("symbol", query_syms).gte("ex_date", anchor).lte("ex_date", end_date)
        res = q.order("ex_date").execute()
        for r in list(getattr(res, "data", None) or []):
            sym = str(r.get("symbol") or "").strip().upper()
            if sym:
                ctx["calendar"].setdefault(sym, []).append(r)
        ctx["calendar_lookahead_days"] = lookahead
        ctx["calendar_window_end"] = end_date
    except Exception as exc:  # noqa: BLE001
        logger.info("corporate_actions_calendar read failed: %s", exc)
    return ctx


def _resolve_symbol_gates(
    client: Any,
    *,
    symbol: str,
    exchange: str,
    eod_ctx: dict[str, Any],
    session_move: float,
    absorption: float,
) -> list[dict[str, Any]]:
    """Per-symbol shadow gates: v2-risk, calendar, delivery/ban/futures, pledge stub."""
    gates: list[dict[str, Any]] = [
        _v2_risk_gate(symbol, eod_ctx),
        _calendar_event_gate(symbol, eod_ctx),
        _delivery_churn_gate(symbol, eod_ctx, session_move=session_move),
        _ban_veto_gate(symbol, eod_ctx),
        _futures_oi_gate(symbol, eod_ctx, session_move=session_move),
    ]
    pledge = _pledge_slb_gate(symbol, eod_ctx)
    if isinstance(pledge, dict):
        gates.append(pledge)
    return [g for g in gates if isinstance(g, dict)]


# ---- STEP 3b (P1-6): reconcile signal_v2 risk into rank_score (not post-hoc withhold) ----
_V2_RISK_SUPPRESS = ("trim", "exit-risk")
_V2_RISK_DAMP_MULT = 0.5
_V2_RANK_BONUS_MAX = 1.5
_V2_RANK_PENALTY_MAX = 3.0
_V2_RANK_TRIM_THRESHOLD = 5.0
_V2_LABEL_RISK_NET: dict[str, float] = {
    "buy": 1.0,
    "accumulate": 2.5,
    "hold": 3.0,
    "trim": 5.5,
    "exit-risk": 8.0,
}
_EXTREME_MOMENTUM_1W_PCT = 10.0
_EXTREME_MOMENTUM_RANK = 5
_RANK_LOOKBACK_CALENDAR_DAYS = 400
_MIN_EMA200_SESSIONS = 250
_PCTILE_1W_REF_RETURN = 11.0  # maps p100 -> ~12.1 pts (equiv to 11% * 1.1)
_PCTILE_1M_REF_RETURN = 11.0  # maps p100 -> ~5.0 pts (equiv to 11% * 0.45)
_PCTILE_MEDIAN_DEFAULT = 50.0
_V2_RANK_VOL_DAMP_FRAC = 0.25  # damp v2 penalty when overextension already penalized


def _parse_v2_risk_net(raw: Any) -> float:
    if raw is None:
        return float("nan")
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if not math.isnan(val) else float("nan")
    if isinstance(raw, dict):
        return _safe_float(raw.get("risk_net"))
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return float("nan")
        try:
            parsed = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            return float("nan")
        return _safe_float(parsed.get("risk_net")) if isinstance(parsed, dict) else float("nan")
    return float("nan")


def _resolve_v2_signal(symbol: str, eod_ctx: dict[str, Any]) -> dict[str, Any]:
    """Latest v2 label + risk_net for a symbol, following NSE rename aliases."""
    sym = str(symbol).strip().upper()
    labels = eod_ctx.get("v2_labels") or {}
    risk_map = eod_ctx.get("v2_risk_net") or {}
    dates_map = eod_ctx.get("v2_label_dates") or {}
    label = ""
    risk_net = float("nan")
    trade_date: str | None = None
    alias_used: str | None = None
    for variant in symbol_lookup_variants(sym):
        if not label and variant in labels:
            label = str(labels[variant]).strip().lower()
            trade_date = dates_map.get(variant)
            if variant != sym:
                alias_used = variant
        if math.isnan(risk_net) and variant in risk_map:
            risk_net = _safe_float(risk_map[variant])
            if variant != sym and alias_used is None:
                alias_used = variant
    if math.isnan(risk_net) and label:
        risk_net = _V2_LABEL_RISK_NET.get(label, float("nan"))
    return {
        "label": label or None,
        "risk_net": _round_or_none(risk_net, 2),
        "trade_date": trade_date,
        "alias_used": alias_used,
    }


def cap_rank_penalty_families(families: dict[str, float]) -> dict[str, float]:
    """Cap each risk family at ``TITAN_V2_RANK_PENALTY_FAMILY_CAP`` (default 30%) of total penalty."""
    cap_frac = _env_float("TITAN_V2_RANK_PENALTY_FAMILY_CAP", 0.30)
    cleaned = {k: max(0.0, float(v)) for k, v in families.items()}
    total = sum(cleaned.values())
    if total <= 0.0 or cap_frac <= 0.0:
        return cleaned
    max_each = total * cap_frac
    return {k: min(v, max_each) for k, v in cleaned.items()}


def _v2_rank_adjustment(
    v2: dict[str, Any],
    *,
    overextension_penalty: float = 0.0,
    penalty_families: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Translate signal_v2 risk_net into a rank_score bonus/penalty term."""
    label = str(v2.get("label") or "").strip().lower()
    risk_net = _safe_float(v2.get("risk_net"))
    if not label and math.isnan(risk_net):
        return {"adjustment": 0.0, "label": None, "risk_net": None, "mode": "no_data"}
    if math.isnan(risk_net):
        risk_net = _V2_LABEL_RISK_NET.get(label, float("nan"))
    if math.isnan(risk_net):
        return {"adjustment": 0.0, "label": label or None, "risk_net": None, "mode": "no_data"}
    bonus_max = _env_float("TITAN_V2_RANK_BONUS_MAX", _V2_RANK_BONUS_MAX)
    penalty_max = _env_float("TITAN_V2_RANK_PENALTY_MAX", _V2_RANK_PENALTY_MAX)
    trim_at = _env_float("TITAN_V2_RANK_TRIM_THRESHOLD", _V2_RANK_TRIM_THRESHOLD)
    vol_damp = _env_float("TITAN_V2_RANK_VOL_DAMP_FRAC", _V2_RANK_VOL_DAMP_FRAC)
    if risk_net < trim_at:
        frac = 1.0 - (risk_net / trim_at) if trim_at > 0 else 0.0
        adj = bonus_max * _clamp(frac, 0.0, 1.0)
        mode = "bonus"
    else:
        span = max(1.0, 10.0 - trim_at)
        frac = (risk_net - trim_at) / span
        adj = -penalty_max * _clamp(frac, 0.0, 1.0)
        mode = "penalty"
        if penalty_families:
            capped = cap_rank_penalty_families(penalty_families)
            capped_total = sum(capped.values())
            raw_total = sum(max(0.0, float(v)) for v in penalty_families.values())
            if raw_total > 0.0 and capped_total < raw_total:
                adj *= capped_total / raw_total
                mode = "penalty_family_capped"
        if overextension_penalty > 0.05 and vol_damp > 0.0:
            adj *= max(0.0, 1.0 - vol_damp)
            mode = "penalty_vol_damped" if mode == "penalty" else mode
    return {
        "adjustment": round(adj, 4),
        "label": label or None,
        "risk_net": round(risk_net, 2),
        "mode": mode,
        "trade_date": v2.get("trade_date"),
        "alias_used": v2.get("alias_used"),
    }


def _v2_risk_gate(symbol: str, eod_ctx: dict[str, Any]) -> dict[str, Any]:
    """Log v2-risk posture; scoring uses _v2_rank_adjustment (no double penalty).

    Withhold in skip mode only for extreme exit-risk (risk_net >= 7). NaN-safe: no label -> no-op.
    """
    mode = _gate_mode("TITAN_V2_RISK_GATE_MODE")
    v2 = _resolve_v2_signal(symbol, eod_ctx)
    label = str(v2.get("label") or "").strip().lower()
    risk_net = _safe_float(v2.get("risk_net"))
    triggered = label in _V2_RISK_SUPPRESS
    extreme = label == "exit-risk" or ((not math.isnan(risk_net)) and risk_net >= 7.0)
    withhold = mode == "skip" and extreme
    would = "allow"
    if withhold:
        would = f"withhold extreme ({label})"
    elif triggered:
        would = "logged (scoring reconciled)"
    return {
        "gate": "v2_risk_label",
        "mode": mode,
        "triggered": triggered,
        "would": would,
        "v2_label": label or None,
        "v2_risk_net": v2.get("risk_net"),
        "v2_label_date": v2.get("trade_date"),
        "score_multiplier": 1.0,
        "withhold": withhold,
        "scoring_reconciled": True,
    }


def _priority_momentum_cap_override(row: dict[str, Any]) -> bool:
    """Allow large/mid caps into priority slots on extreme weekly momentum."""
    threshold = _env_float("TITAN_PRIORITY_MOMENTUM_1W_PCT", _EXTREME_MOMENTUM_1W_PCT)
    rank_limit = max(1, int(_env_float("TITAN_PRIORITY_MOMENTUM_RANK", float(_EXTREME_MOMENTUM_RANK))))
    bucket = str(row.get("market_cap_bucket") or "")
    if bucket not in ("large", "mid"):
        return False
    ret_1w = _safe_float(row.get("return_1w_pct"))
    rank = int(row.get("rank_in_sector") or 999)
    return (not math.isnan(ret_1w)) and ret_1w > threshold and rank <= rank_limit


# ---- Phase 3: corporate-actions / results calendar gate ----
_CALENDAR_EVENT_KEYWORDS = ("dividend", "result")
_CALENDAR_LOOKAHEAD_DAYS = 5
_CALENDAR_DAMP_MULT = 0.5


def _calendar_purpose_is_event(purpose: str) -> bool:
    p = str(purpose or "").strip().lower()
    return any(kw in p for kw in _CALENDAR_EVENT_KEYWORDS)


def _calendar_event_gate(symbol: str, eod_ctx: dict[str, Any]) -> dict[str, Any]:
    """Damp/withhold buys ahead of dividend or results ex-dates (deep-scan P1-1).

    Reads ``corporate_actions_calendar`` (NSE corp-actions ingest). NaN-safe: empty
    table / no matching rows -> no-op. Default mode is shadow (records only).
    """
    mode = _gate_mode("TITAN_CALENDAR_GATE_MODE")
    damp = _env_float("TITAN_CALENDAR_GATE_DAMP_MULT", _CALENDAR_DAMP_MULT)
    lookahead = max(0, int(_env_float("TITAN_CALENDAR_LOOKAHEAD_DAYS", float(_CALENDAR_LOOKAHEAD_DAYS))))
    events = list((eod_ctx.get("calendar") or {}).get(str(symbol).upper()) or [])
    matched = [e for e in events if _calendar_purpose_is_event(str(e.get("purpose") or ""))]
    reasons: list[str] = []
    for ev in matched:
        reasons.append(f"{ev.get('ex_date')}: {str(ev.get('purpose') or '')[:80]}")
    triggered = bool(matched)
    mult, withhold = _gate_effect(mode, triggered, damp)
    return {
        "gate": "calendar_event",
        "mode": mode,
        "triggered": triggered,
        "would": "damp/withhold (upcoming event)" if triggered else "allow",
        "reasons": reasons,
        "lookahead_days": lookahead,
        "window_end": eod_ctx.get("calendar_window_end"),
        "n_events": len(matched),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


# ---- Phase 3: promoter-pledge / SLB borrow gate (stub — no data source yet) ----
def _pledge_slb_gate(symbol: str, eod_ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Placeholder for promoter-pledge spike + SLB borrow-trend distress flags.

    NSE pledge disclosures are quarterly PDFs and SLB borrow trends need a dedicated
    ingest table; neither exists in Supabase yet. When the env knob is not ``off``,
    shadow-logs ``not_implemented`` so rollout wiring is in place before data lands.
    """
    mode = _gate_mode("TITAN_PLEDGE_SLB_GATE_MODE", default="off")
    if mode == "off":
        return None
    logger.info(
        "pledge_slb gate not_implemented for %s (mode=%s; awaiting pledge/SLB ingest)",
        symbol,
        mode,
    )
    return {
        "gate": "pledge_slb",
        "mode": mode,
        "triggered": False,
        "would": "allow",
        "status": "not_implemented",
        "reason": "no pledge/SLB time-series in Supabase yet",
        "score_multiplier": 1.0,
        "withhold": False,
    }


def _breeze_data_freshness_gate() -> dict[str, Any]:
    """Market-wide gate when Breeze session/token is stale (Phase 3 data freshness).

    Shadow mode logs the would-be withhold without changing ranks. When
    ``TITAN_BREEZE_STALE_HARD_STOP=1`` and mode is ``skip``, stale data triggers a
    real withhold (for enforce rollout later).
    """
    from breeze_client import breeze_data_stale_reason, breeze_stale_hard_stop_enabled, is_breeze_data_stale

    mode = _gate_mode("TITAN_BREEZE_FRESHNESS_GATE_MODE", default="shadow")
    stale = is_breeze_data_stale()
    reason = breeze_data_stale_reason() if stale else ""
    hard = breeze_stale_hard_stop_enabled()
    triggered = stale
    if stale:
        logger.warning("data_stale withhold (shadow): %s", reason or "breeze session unavailable")
    if triggered and hard and mode == "skip":
        mult, withhold = 0.0, True
    else:
        mult, withhold = _gate_effect(mode, triggered, 0.5)
    return {
        "gate": "data_freshness",
        "mode": mode,
        "triggered": triggered,
        "would": "withhold (stale Breeze data)" if triggered else "allow",
        "stale": stale,
        "reason": reason or None,
        "hard_stop": hard,
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


# ---- STEP 4b: delivery% / churn gate (delivery_daily) ----
_DELIVERY_FLOOR_PCT = 35.0       # trailing-avg delivery% below this = churn-heavy
_DELIVERY_FALL_PCT = 12.0        # delivery% drop (avg->latest) that counts as falling
_DELIVERY_LOOKBACK = 5
_DELIVERY_DAMP_MULT = 0.5


def _delivery_churn_gate(symbol: str, eod_ctx: dict[str, Any], *, session_move: float) -> dict[str, Any]:
    """Penalise/withhold a buy that is high-volume but LOW or FALLING delivery% -- churn,
    not real accumulation (deep-scan miss: DIXON). NaN-safe: no delivery rows -> no-op.
    """
    mode = _gate_mode("TITAN_DELIVERY_GATE_MODE")
    floor = _env_float("TITAN_DELIVERY_FLOOR_PCT", _DELIVERY_FLOOR_PCT)
    fall = _env_float("TITAN_DELIVERY_FALL_PCT", _DELIVERY_FALL_PCT)
    lookback = max(2, int(_env_float("TITAN_DELIVERY_LOOKBACK", float(_DELIVERY_LOOKBACK))))
    damp = _env_float("TITAN_DELIVERY_GATE_DAMP_MULT", _DELIVERY_DAMP_MULT)

    rows = list((eod_ctx.get("delivery") or {}).get(str(symbol).upper()) or [])
    rows.sort(key=lambda r: str(r.get("trade_date") or ""))
    window = rows[-lookback:]
    dvals = [_safe_float(r.get("deliv_per")) for r in window]
    dvals = [v for v in dvals if not math.isnan(v)]
    latest = dvals[-1] if dvals else float("nan")
    avg = sum(dvals) / len(dvals) if dvals else float("nan")
    reasons: list[str] = []
    low = (not math.isnan(avg)) and avg < floor
    falling = (not math.isnan(latest) and not math.isnan(avg)) and (avg - latest) >= fall
    if low:
        reasons.append(f"avg delivery {avg:.0f}% < floor {floor:.0f}%")
    if falling:
        reasons.append(f"delivery falling avg {avg:.0f}%->{latest:.0f}%")
    triggered = low or falling
    mult, withhold = _gate_effect(mode, triggered, damp)
    return {
        "gate": "delivery_churn",
        "mode": mode,
        "triggered": triggered,
        "would": "damp/withhold (churn)" if triggered else "allow",
        "reasons": reasons,
        "delivery_latest": _round_or_none(latest, 2),
        "delivery_avg": _round_or_none(avg, 2),
        "n_sessions": len(dvals),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


# ---- STEP 4c: F&O ban-list veto (fno_ban_daily) ----
def _ban_veto_gate(symbol: str, eod_ctx: dict[str, Any]) -> dict[str, Any]:
    """Hard buy-withhold veto for an F&O-banned name. NaN-safe: empty ban set -> no-op.

    Ban is a hard regulatory state, so when enforced the default is to withhold (not just
    damp); in shadow it only records the would-be veto.
    """
    mode = _gate_mode("TITAN_BAN_GATE_MODE")
    banned = eod_ctx.get("ban") or set()
    triggered = str(symbol).upper() in banned
    # Ban veto enforces a withhold (skip-strength) rather than a soft damp.
    if not triggered or mode in ("off", "shadow"):
        mult, withhold = 1.0, False
    elif mode == "damp":
        mult, withhold = _env_float("TITAN_BAN_GATE_DAMP_MULT", 0.5), False
    else:  # skip
        mult, withhold = 0.0, True
    return {
        "gate": "fno_ban",
        "mode": mode,
        "triggered": triggered,
        "would": "withhold (in F&O ban)" if triggered else "allow",
        "ban_date": eod_ctx.get("ban_date"),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


# ---- STEP 4d: futures OI short-covering vs long-buildup flag (futures_daily) ----
def _futures_oi_gate(symbol: str, eod_ctx: dict[str, Any], *, session_move: float) -> dict[str, Any]:
    """Flag a price pop driven by short-covering (price up + OI down) rather than fresh
    long buildup (price up + OI up). Short-covering pops are less durable, so the flag
    damps; long buildup is informational only. NaN-safe: <2 futures rows -> no-op.
    """
    mode = _gate_mode("TITAN_FUTURES_GATE_MODE")
    damp = _env_float("TITAN_FUTURES_GATE_DAMP_MULT", 0.75)
    rows = list((eod_ctx.get("futures") or {}).get(str(symbol).upper()) or [])
    rows.sort(key=lambda r: str(r.get("trade_date") or ""))
    structure = "unknown"
    triggered = False
    oi_chg = float("nan")
    if len(rows) >= 1:
        oi_chg = _safe_float(rows[-1].get("change_in_oi"))
    price_up = (not math.isnan(session_move)) and session_move > 0.0
    price_dn = (not math.isnan(session_move)) and session_move < 0.0
    if not math.isnan(oi_chg):
        if price_up and oi_chg > 0:
            structure = "long_buildup"
        elif price_up and oi_chg < 0:
            structure = "short_covering"
            triggered = True
        elif price_dn and oi_chg > 0:
            structure = "short_buildup"
        elif price_dn and oi_chg < 0:
            structure = "long_unwinding"
    mult, withhold = _gate_effect(mode, triggered, damp)
    return {
        "gate": "futures_oi",
        "mode": mode,
        "triggered": triggered,
        "would": "damp (short-covering pop)" if triggered else "allow",
        "structure": structure,
        "change_in_oi": _round_or_none(oi_chg, 0),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


def _gate_record_applied(record: dict[str, Any]) -> bool:
    """True when a gate record is triggered and its mode is actively enforced."""
    if not bool(record.get("triggered")):
        return False
    mode = str(record.get("mode") or "shadow").strip().lower()
    if mode in ("off", "shadow"):
        return False
    if mode in ("damp", "skip", "enforce"):
        return True
    if bool(record.get("withhold")):
        return True
    try:
        mult = float(record.get("score_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    return mult < 1.0 - 1e-9


_INSTITUTIONAL_DAMP_MULT = 0.85


def _institutional_gate(eod_ctx: dict[str, Any]) -> dict[str, Any]:
    """Market-level FII risk-off backdrop gate (STEP 4d enforce path).

    Triggered when FII net flow is negative; damp/skip modes affect rank_score via
    ``TITAN_INSTITUTIONAL_GATE_MODE``. NaN-safe: missing FII -> no-op.
    """
    mode = _gate_mode("TITAN_INSTITUTIONAL_GATE_MODE")
    damp = _env_float("TITAN_INSTITUTIONAL_GATE_DAMP_MULT", _INSTITUTIONAL_DAMP_MULT)
    inst = eod_ctx.get("institutional") or {}
    fii = _safe_float(inst.get("fii_net_crs"))
    dii = _safe_float(inst.get("dii_net_crs"))
    triggered = (not math.isnan(fii)) and fii < 0.0
    reasons: list[str] = []
    if triggered:
        reasons.append(f"FII net {fii:+.0f} Cr")
        if not math.isnan(dii):
            reasons.append(f"DII net {dii:+.0f} Cr")
    mult, withhold = _gate_effect(mode, triggered, damp)
    return {
        "gate": "institutional",
        "mode": mode,
        "triggered": triggered,
        "would": "damp (institutional backdrop)" if triggered else "allow",
        "reasons": reasons,
        "fii_net_crs": _round_or_none(fii, 2),
        "dii_net_crs": _round_or_none(dii, 2),
        "score_multiplier": round(mult, 4),
        "withhold": withhold,
    }


def _institutional_context(
    eod_ctx: dict[str, Any], *, gate: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Market-level FII/DII net flow as a separate institutional_score input (STEP 4d).

    Includes the institutional gate decision for digest/ranking meta. NaN-safe.
    """
    gate = gate or _institutional_gate(eod_ctx)
    inst = eod_ctx.get("institutional") or {}
    fii = _safe_float(inst.get("fii_net_crs"))
    dii = _safe_float(inst.get("dii_net_crs"))
    score = float("nan")
    if not math.isnan(fii) or not math.isnan(dii):
        score = (0.0 if math.isnan(fii) else fii) + (0.0 if math.isnan(dii) else dii)
    return {
        "as_of_date": inst.get("as_of_date"),
        "fii_net_crs": gate.get("fii_net_crs"),
        "dii_net_crs": gate.get("dii_net_crs"),
        "institutional_score": _round_or_none(score, 2),
        "risk_off": bool(gate.get("triggered")),
        "mode": gate.get("mode"),
        "gate_applied": _gate_record_applied(gate),
    }


def rehydrate_persisted_gate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Re-apply current gate env modes to a persisted ranking gate record.

    Weekend ranking refresh often stores ``mode=shadow`` with multiplier 1.0 even when
    production CI runs with damp/skip. Digest bridging uses this so Context lines reflect
    the active runtime policy without recomputing full rankings.
    """
    if not isinstance(record, dict) or not bool(record.get("triggered")):
        return record
    out = dict(record)
    stored_mode = str(out.get("mode") or "shadow").strip().lower()
    if stored_mode in ("damp", "skip", "enforce"):
        out["applied"] = _gate_record_applied(out)
        return out
    gate = str(out.get("gate") or "").strip().lower()
    if gate == "fno_ban":
        mode = _gate_mode("TITAN_BAN_GATE_MODE")
        if mode in ("off", "shadow"):
            mult, withhold = 1.0, False
        elif mode == "damp":
            mult, withhold = _env_float("TITAN_BAN_GATE_DAMP_MULT", 0.5), False
        else:
            mult, withhold = 0.0, True
    else:
        spec = {
            "sector_regime": ("TITAN_REGIME_GATE_MODE", "TITAN_REGIME_GATE_DAMP_MULT", _REGIME_DAMP_MULT),
            "delivery_churn": ("TITAN_DELIVERY_GATE_MODE", "TITAN_DELIVERY_GATE_DAMP_MULT", _DELIVERY_DAMP_MULT),
            "institutional": (
                "TITAN_INSTITUTIONAL_GATE_MODE",
                "TITAN_INSTITUTIONAL_GATE_DAMP_MULT",
                _INSTITUTIONAL_DAMP_MULT,
            ),
            "futures_oi": ("TITAN_FUTURES_GATE_MODE", "TITAN_FUTURES_GATE_DAMP_MULT", 0.75),
            "v2_risk": ("TITAN_V2_RISK_GATE_MODE", "TITAN_V2_RISK_DAMP_MULT", _V2_RISK_DAMP_MULT),
            "calendar_event": ("TITAN_CALENDAR_GATE_MODE", "TITAN_CALENDAR_GATE_DAMP_MULT", _CALENDAR_DAMP_MULT),
            "breeze_freshness": ("TITAN_BREEZE_FRESHNESS_GATE_MODE", "TITAN_BREEZE_FRESHNESS_GATE_DAMP_MULT", 0.5),
            "data_freshness": ("TITAN_BREEZE_FRESHNESS_GATE_MODE", "TITAN_BREEZE_FRESHNESS_GATE_DAMP_MULT", 0.5),
        }.get(gate)
        if not spec:
            return out
        env_mode, env_damp, default_damp = spec
        mode = _gate_mode(env_mode)
        damp = _env_float(env_damp, default_damp)
        mult, withhold = _gate_effect(mode, True, damp)
    out["mode"] = mode
    out["score_multiplier"] = round(mult, 4)
    out["withhold"] = withhold
    out["applied"] = _gate_record_applied(out)
    return out


def rehydrate_persisted_gate_records(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in records or []:
        if isinstance(item, dict):
            out.append(rehydrate_persisted_gate_record(item))
    return out


def rehydrate_institutional_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh institutional gate mode/effect from current env for digest bridging."""
    if not isinstance(ctx, dict) or not bool(ctx.get("risk_off")):
        return ctx
    out = dict(ctx)
    stored_mode = str(out.get("mode") or "shadow").strip().lower()
    if stored_mode in ("damp", "skip", "enforce"):
        out["gate_applied"] = _gate_record_applied(
            {
                "gate": "institutional",
                "mode": stored_mode,
                "triggered": True,
                "score_multiplier": out.get("score_multiplier", 1.0),
                "withhold": bool(out.get("withhold")),
            }
        )
        return out
    gate = _institutional_gate(
        {
            "institutional": {
                "fii_net_crs": out.get("fii_net_crs"),
                "dii_net_crs": out.get("dii_net_crs"),
            }
        }
    )
    out["mode"] = gate.get("mode")
    out["score_multiplier"] = gate.get("score_multiplier")
    out["withhold"] = gate.get("withhold")
    out["gate_applied"] = _gate_record_applied(gate)
    return out


def build_sector_rankings(
    cfg: TitanConfig,
    *,
    sector_key: str,
    instruments: list[SectorInstrument],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    from breeze_client import BreezeDataStaleError, create_breeze_session

    breeze = None
    try:
        breeze = create_breeze_session(cfg)
    except BreezeDataStaleError as exc:
        logger.warning("Breeze session unavailable; ranking without live prices: %s", exc)
    freshness_gate = _breeze_data_freshness_gate()
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
    sector_key_norm = sector_key.strip().lower()
    sector_blend_points = _news_blend_points(sector_news_score)
    coverage_pairs = sorted(
        {
            (str(inst.symbol).strip().upper(), str(inst.exchange).strip().upper())
            for inst in instruments
            if str(inst.symbol).strip() and str(inst.exchange).strip().upper() in ("NSE", "BSE")
        }
    )
    live_fetch_pairs = _select_live_fetch_pairs(coverage_pairs)
    stock_news_by_symbol = resolve_stock_news_batch(
        cfg,
        pairs=coverage_pairs,
        allow_live_fetch_for=live_fetch_pairs,
    )
    resolved_stock_pairs = set(stock_news_by_symbol.keys())
    # Shadow-mode gates context (read-only; NaN-safe). Regime is per-sector; the EOD /
    # v2-risk gates are per-symbol and resolved inside the loop.
    gate_client = create_client(cfg.supabase_url, cfg.supabase_key)
    regime = _fetch_sector_regime(gate_client, sector_key=sector_key, as_of_date=as_of_date)
    regime_hostile = bool(regime.get("triggered"))
    eod_ctx = _fetch_eod_gate_context(
        gate_client,
        symbols=[inst.symbol for inst in instruments],
        as_of_date=as_of_date,
    )
    institutional_gate = _institutional_gate(eod_ctx)
    institutional_ctx = _institutional_context(eod_ctx, gate=institutional_gate)
    pending: list[dict[str, Any]] = []
    rank_lookback = int(_env_float("TITAN_RANK_LOOKBACK_DAYS", float(_RANK_LOOKBACK_CALENDAR_DAYS)))
    for inst in instruments:
        issues: list[str] = []
        symbol_u = str(inst.symbol).strip().upper()
        try:
            if breeze is None:
                raise BreezeDataStaleError("Breeze session unavailable")
            df = fetch_equity_data(
                cfg,
                inst.symbol,
                inst.exchange,
                breeze=breeze,
                lookback_calendar_days=rank_lookback,
                max_retries=2,
            )
        except BreezeDataStaleError as exc:
            logger.warning("Ranking data fetch skipped for %s (%s): %s", inst.symbol, inst.exchange, exc)
            df = pd.DataFrame()
            issues.append("breeze_data_stale")
        except Exception as exc:
            logger.warning("Ranking data fetch failed for %s (%s): %s", inst.symbol, inst.exchange, exc)
            df = pd.DataFrame()
            issues.append("price_history_fetch_error")
        close_col = "close" if "close" in df.columns else (df.columns[-1] if len(df.columns) > 0 else None)
        series = pd.to_numeric(df[close_col], errors="coerce") if close_col is not None else pd.Series(dtype=float)
        ret_1w = _return_pct(series, periods_back=5)
        ret_1m = _return_pct(series, periods_back=20)
        ret_3m = _return_pct(series, periods_back=60)
        ret_1d = _return_pct(series, periods_back=1)  # latest session move (P0-2 sign-gate)
        absorption = volume_participation_ratio(df) if not df.empty else float("nan")
        # Fix A inputs: ATR-normalized EMA200 stretch (NaN-safe; needs ~250 sessions +
        # OHLC for reliable EMA200/ATR stretch).
        ema_dist, stretch = _stretch_inputs_from_df(df, series)
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
        pending.append(
            {
                "inst": inst,
                "symbol_u": symbol_u,
                "issues": issues,
                "df": df,
                "ret_1w": ret_1w,
                "ret_1m": ret_1m,
                "ret_3m": ret_3m,
                "ret_1d": ret_1d,
                "absorption": absorption,
                "stretch": stretch,
                "ema_dist": ema_dist,
                "market_cap_cr": market_cap_cr,
                "market_cap_source": market_cap_source,
                "bucket": bucket,
            }
        )

    intent_vals = [
        _safe_float((eod_ctx.get("intent_scores") or {}).get(str(p["symbol_u"]).upper()))
        for p in pending
    ]
    nw_vals = [
        _safe_float((eod_ctx.get("next_week_scores") or {}).get(str(p["symbol_u"]).upper()))
        for p in pending
    ]
    valid_intent = [v for v in intent_vals if not math.isnan(v)]
    valid_nw = [v for v in nw_vals if not math.isnan(v)]
    ret_1m_cohort = [_safe_float(p.get("ret_1m")) for p in pending if not math.isnan(_safe_float(p.get("ret_1m")))]
    med_1m = float(sum(ret_1m_cohort) / len(ret_1m_cohort)) if ret_1m_cohort else float("nan")
    for p, intent_v, nw_v in zip(pending, intent_vals, nw_vals):
        r1m = _safe_float(p.get("ret_1m"))
        p["rel_strength"] = (r1m - med_1m) if not (math.isnan(r1m) or math.isnan(med_1m)) else float("nan")
        p["percentile_intent"] = (
            50.0 if math.isnan(intent_v) else percentile_rank_0_100(valid_intent, intent_v)
        ) if valid_intent else 50.0
        p["percentile_next_week"] = (
            50.0 if math.isnan(nw_v) else percentile_rank_0_100(valid_nw, nw_v)
        ) if valid_nw else 50.0
    _cohort_return_percentiles(pending)
    rows: list[dict[str, Any]] = []
    for p in pending:
        inst = p["inst"]
        symbol_u = p["symbol_u"]
        issues = p["issues"]
        df = p["df"]
        ret_1w = p["ret_1w"]
        ret_1m = p["ret_1m"]
        ret_1d = p["ret_1d"]
        absorption = p["absorption"]
        stretch = p["stretch"]
        ema_dist = p["ema_dist"]
        market_cap_cr = p["market_cap_cr"]
        market_cap_source = p["market_cap_source"]
        bucket = p["bucket"]
        pct_1w = _safe_float(p.get("percentile_1w"))
        pct_1m = _safe_float(p.get("percentile_1m"))
        pct_inputs = {
            "sector_pctile_return_1m": pct_1m,
            "sector_pctile_return_3m": _safe_float(p.get("percentile_3m")),
            "sector_pctile_rel_strength": _safe_float(p.get("percentile_rel_strength")),
            "sector_pctile_intent": _safe_float(p.get("percentile_intent")),
            "sector_pctile_next_week": _safe_float(p.get("percentile_next_week")),
        }
        srm_score = compute_sector_relative_momentum_score(**pct_inputs)
        srr_score = compute_sector_relative_rank_score(**pct_inputs)
        srr_components = compute_sector_relative_rank_components(**pct_inputs)
        overext = _overextension_penalty(
            ret_1w=ret_1w,
            ret_1m=ret_1m,
            absorption=absorption,
            stretch=stretch,
            ema_dist=ema_dist,
            regime_hostile=regime_hostile,
        )
        absorption_bd = _absorption_term(absorption, ret_1d)
        base_score = _score_from_features(
            bucket=bucket,
            ret_1w=ret_1w,
            ret_1m=ret_1m,
            absorption=absorption,
            stretch=stretch,
            ema_dist=ema_dist,
            regime_hostile=regime_hostile,
            session_move=ret_1d,
            percentile_1w=pct_1w,
            percentile_1m=pct_1m,
            sector_relative_rank_score=srr_score,
            sector_pctile_return_1m=pct_1m,
            sector_pctile_return_3m=_safe_float(p.get("percentile_3m")),
            sector_pctile_rel_strength=_safe_float(p.get("percentile_rel_strength")),
            sector_pctile_intent=_safe_float(p.get("percentile_intent")),
            sector_pctile_next_week=_safe_float(p.get("percentile_next_week")),
        )
        exchange_u = str(inst.exchange).strip().upper()
        stock_pair = (symbol_u, exchange_u)
        stock_news_meta = stock_news_by_symbol.get(stock_pair, {})
        stock_items = stock_news_meta.get("items") if isinstance(stock_news_meta.get("items"), list) else []
        stock_aliases = stock_news_meta.get("aliases")
        if not isinstance(stock_aliases, list):
            stock_aliases = _instrument_alias_candidates(cfg, symbol=symbol_u, exchange=inst.exchange)
        stock_corr = correlate_stock_news_with_macro(
            symbol=symbol_u,
            sector_key=sector_key_norm,
            stock_news_items=stock_items,
            snapshot=snapshot,
            aliases=stock_aliases,
            stock_news_fetch_error=str(stock_news_meta.get("error") or "").strip(),
        )
        stock_news_score = _safe_float(stock_corr.get("stock_news_score"))
        if math.isnan(stock_news_score):
            stock_news_score = 0.0
        net_news_score = _safe_float(stock_corr.get("net_score"))
        if math.isnan(net_news_score):
            net_news_score = sector_news_score
        row_blend_points = _news_blend_points(net_news_score)
        stock_coverage = _stock_news_coverage_status(
            pair=stock_pair,
            stock_news_meta=stock_news_meta if isinstance(stock_news_meta, dict) else {},
            resolved_keys=resolved_stock_pairs,
        )
        pre_gate_score = round(base_score + row_blend_points, 4)
        v2_signal = _resolve_v2_signal(symbol_u, eod_ctx)
        v2_rank = _v2_rank_adjustment(
            v2_signal,
            overextension_penalty=float(overext.get("penalty") or 0.0),
        )
        pre_gate_score = round(pre_gate_score + float(v2_rank.get("adjustment") or 0.0), 4)
        # Shadow-mode buy-suppression gates (regime + delivery/ban/futures/v2-risk). In
        # the default shadow mode the multiplier is 1.0 / withhold False, so the published
        # rank is unchanged and only the would-be decisions are recorded in meta.
        symbol_gates = _resolve_symbol_gates(
            gate_client,
            symbol=symbol_u,
            exchange=inst.exchange,
            eod_ctx=eod_ctx,
            session_move=ret_1d,
            absorption=absorption,
        )
        gate_records = [regime, freshness_gate, institutional_gate] + symbol_gates
        gate_mult, gate_withhold = _combine_gate_effects(gate_records)
        score = round(pre_gate_score * gate_mult, 4)
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
            "stock_news_score": round(stock_news_score, 4),
            "net_news_score": round(net_news_score, 4),
            "sector_blend_points": sector_blend_points,
            "blend_points": row_blend_points,
            "blend_weight": _news_blend_weight(),
            "blend_cap": _news_blend_cap(),
            "confidence": round(_safe_float(sector_news.get("confidence")), 4),
            "drivers_boosting": sector_news.get("drivers_boosting") or [],
            "drivers_dragging": sector_news.get("drivers_dragging") or [],
            "drivers_top": sector_news.get("drivers_top") or [],
            "stock_news_fetched_count": len(stock_items),
            "stock_news_coverage": stock_coverage,
            "stock_news_driver": str(stock_corr.get("driver") or "").strip(),
            "stock_news_direction": str(stock_corr.get("direction") or "neutral"),
            "stock_news_fallback_label": str(stock_corr.get("fallback_label") or "").strip(),
            "stock_news_top_headlines": (
                (stock_corr.get("evidence") or {}).get("top_headlines", {}).get("stock")
                if isinstance(stock_corr.get("evidence"), dict)
                else []
            ),
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
                    "percentile_1w": _round_or_none(pct_1w, digits=2),
                    "percentile_1m": _round_or_none(pct_1m, digits=2),
                    "percentile_3m": _round_or_none(_safe_float(p.get("percentile_3m")), digits=2),
                    "percentile_rel_strength": _round_or_none(
                        _safe_float(p.get("percentile_rel_strength")), digits=2
                    ),
                    "percentile_intent": _round_or_none(_safe_float(p.get("percentile_intent")), digits=2),
                    "percentile_next_week": _round_or_none(
                        _safe_float(p.get("percentile_next_week")), digits=2
                    ),
                    "sector_relative_momentum_score": srm_score,
                    "sector_relative_rank_score": srr_score,
                    "sector_relative_rank_components": srr_components,
                    "overextension_penalty": overext.get("penalty", 0.0),
                    "overextension_components": overext.get("components", {}),
                    "absorption_term": absorption_bd,
                    "pre_gate_rank_score": pre_gate_score,
                    "v2_rank_adjustment": v2_rank,
                    "gate_multiplier": round(gate_mult, 4),
                    "gate_withhold": gate_withhold,
                    "shadow_gates": gate_records,
                    "institutional_context": institutional_ctx,
                    "news": news_meta,
                },
            }
        )
    ranked = sorted(
        rows,
        key=lambda r: (_safe_float(r.get("rank_score")), _safe_float(r.get("return_1w_pct"))),
        reverse=True,
    )
    n_ranked = len(ranked)
    quartile_cut = max(1, math.ceil(n_ranked * 0.25)) if n_ranked else 1
    for i, row in enumerate(ranked, start=1):
        row["rank_in_sector"] = i
        meta = row.setdefault("meta", {})
        v2_meta = meta.get("v2_rank_adjustment") if isinstance(meta.get("v2_rank_adjustment"), dict) else {}
        label = str(v2_meta.get("label") or "").strip().lower()
        if i <= quartile_cut and label in _V2_RISK_SUPPRESS and v2_meta.get("mode") == "penalty":
            meta["dual_engine_conflict"] = True
            meta["conflict_detail"] = {
                "rank_in_sector": i,
                "quartile_cutoff": quartile_cut,
                "v2_label": label,
                "v2_risk_net": v2_meta.get("risk_net"),
                "technical_rank_score": meta.get("technical_rank_score"),
                "v2_rank_adjustment": v2_meta.get("adjustment"),
            }
    top_n = max(1, int(top_n))
    priority_candidates = [
        r
        for r in ranked
        if int((r.get("meta") or {}).get("rows_count") or 0) > 0
        and not bool((r.get("meta") or {}).get("gate_withhold"))
    ]
    # Primary objective: small/micro-cap AI names for higher-move opportunity.
    preferred = [r for r in priority_candidates if str(r.get("market_cap_bucket")) in ("micro", "small")]
    momentum_override = [r for r in priority_candidates if _priority_momentum_cap_override(r)]
    preferred_keys = {(r["symbol"], r["exchange"]) for r in preferred}
    momentum_keys = {(r["symbol"], r["exchange"]) for r in momentum_override}
    fallback = [
        r
        for r in priority_candidates
        if (r["symbol"], r["exchange"]) not in preferred_keys
        and (r["symbol"], r["exchange"]) not in momentum_keys
    ]
    ordered_candidates: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for bucket in (preferred, momentum_override, fallback):
        for r in bucket:
            key = (r["symbol"], r["exchange"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ordered_candidates.append(r)
    priority_keys = {
        (r["symbol"], r["exchange"])
        for r in ordered_candidates[:top_n]
    }
    for row in ranked:
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


def _priority_rows_to_instruments(data: list[dict[str, Any]]) -> list[SectorInstrument]:
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


def _fetch_priority_ranking_rows(
    client: Any,
    *,
    sector_key: str,
    as_of_date: str,
    top_n: int | None,
) -> list[dict[str, Any]]:
    q = (
        client.table("sector_priority_rankings")
        .select("symbol,exchange,rank_in_sector")
        .eq("sector_key", sector_key)
        .eq("as_of_date", as_of_date)
        .eq("is_priority", True)
        .order("rank_in_sector")
    )
    if top_n is not None:
        q = q.limit(max(1, int(top_n)))
    res = q.execute()
    rows = list(getattr(res, "data", None) or [])
    return [x for x in rows if isinstance(x, dict)]


def _latest_priority_as_of_date(client: Any, *, sector_key: str) -> str | None:
    try:
        res = (
            client.table("sector_priority_rankings")
            .select("as_of_date")
            .eq("sector_key", sector_key)
            .eq("is_priority", True)
            .order("as_of_date", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.warning("Latest priority as_of_date lookup failed for sector=%s: %s", sector_key, exc)
        return None
    rows = list(getattr(res, "data", None) or [])
    if not rows or not isinstance(rows[0], dict):
        return None
    return str(rows[0].get("as_of_date") or "").strip() or None


def load_priority_instruments(
    cfg: TitanConfig,
    *,
    sector_key: str,
    top_n: int | None = None,
) -> list[SectorInstrument]:
    """Load persisted priority symbols for a sector.

    Tries today's IST ``as_of_date`` first, then the latest date with priority rows
    (weekend refresh may not have run yet on a new calendar day).
    """
    from sector_registry import resolve_sector_key

    sector_key = resolve_sector_key(sector_key)
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of_today = datetime.now(IST).date().isoformat()
    try:
        data = _fetch_priority_ranking_rows(
            client,
            sector_key=sector_key,
            as_of_date=as_of_today,
            top_n=top_n,
        )
    except Exception as exc:
        logger.warning("Priority load failed for sector=%s as_of=%s: %s", sector_key, as_of_today, exc)
        data = []
    if not data:
        latest = _latest_priority_as_of_date(client, sector_key=sector_key)
        if latest and latest != as_of_today:
            try:
                data = _fetch_priority_ranking_rows(
                    client,
                    sector_key=sector_key,
                    as_of_date=latest,
                    top_n=top_n,
                )
            except Exception as exc:
                logger.warning(
                    "Priority load failed for sector=%s as_of=%s: %s",
                    sector_key,
                    latest,
                    exc,
                )
                data = []
            if data:
                logger.info(
                    "Priority list for sector=%s: no rows for as_of=%s; using latest as_of=%s (%s symbols)",
                    sector_key,
                    as_of_today,
                    latest,
                    len(data),
                )
    return _priority_rows_to_instruments(data)


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
                    "overextension_penalty": meta.get("overextension_penalty"),
                    "overextension_components": meta.get("overextension_components"),
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

