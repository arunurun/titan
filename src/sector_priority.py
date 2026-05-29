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
_NEWS_STALE_HOURS_DEFAULT = 36.0
_NEWS_SNAPSHOT_TTL_HOURS_DEFAULT = 2.0
_NEWS_FETCH_LIMIT_DEFAULT = 40
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
}


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


def _news_sentiment_score(text: str) -> float:
    t = _normalize_news_text(text)
    if not t:
        return 0.0
    pos_hits = sum(1 for k in _POSITIVE_NEWS_TERMS if k in t)
    neg_hits = sum(1 for k in _NEGATIVE_NEWS_TERMS if k in t)
    raw = (pos_hits - neg_hits) / max(2.0, pos_hits + neg_hits + 1.0)
    return round(_clamp(raw, -1.0, 1.0), 4)


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

