"""Supabase persistence for per-symbol news feed and snapshots."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import create_client

from config_loader import TitanConfig
from news_sentiment import aggregate_sentiment, compute_sentiment, extract_event_type, sentiment_model_name

logger = logging.getLogger(__name__)

_NEWS_MAX_AGE_HOURS_DEFAULT = 36.0
_NEWS_SNAPSHOT_TTL_HOURS_DEFAULT = 2.0
_SYMBOL_SNAPSHOT_TABLE_DEFAULT = "symbol_news_snapshots"


def _news_max_age_hours() -> float:
    raw = (str(os.environ.get("TITAN_NEWS_MAX_AGE_HOURS", "")) or "").strip()
    if not raw:
        return _NEWS_MAX_AGE_HOURS_DEFAULT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _NEWS_MAX_AGE_HOURS_DEFAULT


def _news_snapshot_ttl_seconds() -> int:
    raw = (str(os.environ.get("TITAN_NEWS_SNAPSHOT_TTL_HOURS", "")) or "").strip()
    if not raw:
        return int(_NEWS_SNAPSHOT_TTL_HOURS_DEFAULT * 3600)
    try:
        return max(900, int(float(raw) * 3600))
    except ValueError:
        return int(_NEWS_SNAPSHOT_TTL_HOURS_DEFAULT * 3600)


def _symbol_snapshot_table_name() -> str:
    raw = (str(os.environ.get("TITAN_SYMBOL_NEWS_SNAPSHOT_TABLE", "")) or "").strip()
    if not raw:
        return _SYMBOL_SNAPSHOT_TABLE_DEFAULT
    return raw


def _title_hash(title: str) -> str:
    normalized = hashlib.sha256(str(title or "").strip().lower().encode("utf-8")).hexdigest()
    return normalized


def _content_hash(summary: str) -> str | None:
    txt = str(summary or "").strip()
    if not txt:
        return None
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


def _to_utc_datetime(raw: Any) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _prepare_news_row(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    sentiment = str(item.get("sentiment") or "neutral")
    sentiment_score = float(item.get("sentiment_score") or 0.0)
    sentiment_model = str(item.get("sentiment_model") or sentiment_model_name())
    if sentiment == "neutral" and sentiment_score == 0.0 and title:
        scored = compute_sentiment(f"{title} {summary}")
        sentiment = str(scored.get("sentiment") or "neutral")
        sentiment_score = float(scored.get("score") or 0.0)
        sentiment_model = str(scored.get("model") or sentiment_model)
    event_type = str(item.get("event_type") or extract_event_type(title, summary))
    return {
        "symbol": str(item.get("symbol") or "").strip().upper(),
        "exchange": str(item.get("exchange") or "NSE").strip().upper(),
        "title": title,
        "url": str(item.get("url") or "").strip(),
        "source": str(item.get("source") or "unknown").strip() or "unknown",
        "published_at": str(item.get("published_at") or datetime.now(timezone.utc).isoformat()),
        "summary": summary or None,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "sentiment_model": sentiment_model,
        "relevance_score": float(item.get("relevance_score") or 0.5),
        "impact_level": str(item.get("impact_level") or "medium"),
        "event_type": event_type,
        "is_duplicate": bool(item.get("is_duplicate") or False),
    }


def _upsert_sentiment_cache(
    client: Any,
    *,
    news_id: int,
    item: dict[str, Any],
    sentiment: str,
    sentiment_score: float,
    model_used: str,
    computation_time_ms: float | None = None,
) -> None:
    row = {
        "news_id": int(news_id),
        "title_hash": _title_hash(str(item.get("title") or "")),
        "content_hash": _content_hash(str(item.get("summary") or "")),
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "confidence": abs(float(sentiment_score)),
        "model_used": model_used,
        "computation_time_ms": computation_time_ms,
    }
    try:
        client.table("news_sentiment_cache").upsert(row, on_conflict="news_id").execute()
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        if "could not find the table" in msg.lower():
            logger.info("news_sentiment_cache table missing; cache write skipped")
        else:
            logger.info("news_sentiment_cache upsert skipped: %s", msg)
    except Exception as exc:
        logger.info("news_sentiment_cache upsert skipped: %s", exc)


def _find_title_duplicate_original(
    client: Any,
    *,
    symbol: str,
    title_hash: str,
    exclude_news_id: int,
) -> int | None:
    """Return oldest non-duplicate news_feed id for same symbol + title hash, if any."""
    sym = str(symbol or "").strip().upper()
    if not sym or not title_hash:
        return None
    try:
        res = (
            client.table("news_sentiment_cache")
            .select("news_id")
            .eq("title_hash", title_hash)
            .neq("news_id", int(exclude_news_id))
            .execute()
        )
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        if "could not find the table" not in msg.lower():
            logger.info("title-hash duplicate lookup skipped: %s", msg)
        return None
    except Exception as exc:
        logger.info("title-hash duplicate lookup skipped: %s", exc)
        return None
    cache_rows = list(getattr(res, "data", None) or [])
    candidate_ids: list[int] = []
    for row in cache_rows:
        if not isinstance(row, dict):
            continue
        try:
            candidate_ids.append(int(row.get("news_id")))
        except (TypeError, ValueError):
            continue
    for candidate_id in sorted(candidate_ids):
        try:
            feed_res = (
                client.table("news_feed")
                .select("id")
                .eq("id", candidate_id)
                .eq("symbol", sym)
                .eq("is_duplicate", False)
                .limit(1)
                .execute()
            )
        except APIError:
            continue
        except Exception:
            continue
        feed_rows = list(getattr(feed_res, "data", None) or [])
        if feed_rows and isinstance(feed_rows[0], dict) and feed_rows[0].get("id"):
            return int(feed_rows[0]["id"])
    return None


def store_news_items(
    cfg: TitanConfig,
    items: list[dict[str, Any]],
) -> dict[str, int]:
    """Insert news into news_feed with URL deduplication."""
    if not items:
        return {"inserted": 0, "duplicates_skipped": 0, "updated": 0, "errors": 0}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    inserted = 0
    duplicates_skipped = 0
    updated = 0
    errors = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        row = _prepare_news_row(raw)
        if not row.get("title") or not row.get("url") or not row.get("symbol"):
            errors += 1
            continue
        title_hash = _title_hash(str(row.get("title") or ""))
        try:
            res = client.table("news_feed").upsert(row, on_conflict="url").execute()
            rows = list(getattr(res, "data", None) or [])
            news_id = int(rows[0].get("id")) if rows and isinstance(rows[0], dict) and rows[0].get("id") else 0
            if news_id:
                _upsert_sentiment_cache(
                    client,
                    news_id=news_id,
                    item=row,
                    sentiment=str(row.get("sentiment") or "neutral"),
                    sentiment_score=float(row.get("sentiment_score") or 0.0),
                    model_used=str(row.get("sentiment_model") or sentiment_model_name()),
                )
                original_id = _find_title_duplicate_original(
                    client,
                    symbol=str(row.get("symbol") or ""),
                    title_hash=title_hash,
                    exclude_news_id=news_id,
                )
                if original_id:
                    mark_news_as_duplicate(cfg, news_id, original_id)
                    duplicates_skipped += 1
                    continue
            inserted += 1
        except APIError as exc:
            payload = exc.args[0] if exc.args else {}
            msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
            lower = msg.lower()
            if "duplicate" in lower or "unique" in lower or "23505" in lower:
                duplicates_skipped += 1
            elif "could not find the table" in lower:
                logger.info("news_feed table missing; store skipped")
                return {"inserted": 0, "duplicates_skipped": 0, "updated": 0, "errors": len(items)}
            else:
                logger.warning("news_feed upsert failed url=%s: %s", row.get("url"), msg)
                errors += 1
        except Exception as exc:
            logger.warning("news_feed upsert failed url=%s: %s", row.get("url"), exc)
            errors += 1
    return {
        "inserted": inserted,
        "duplicates_skipped": duplicates_skipped,
        "updated": updated,
        "errors": errors,
    }


def get_recent_news_for_symbol(
    cfg: TitanConfig,
    symbol: str,
    exchange: str = "NSE",
    lookback_hours: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Retrieve cached news for a symbol from news_feed."""
    sym = str(symbol or "").strip().upper()
    ex = str(exchange or "NSE").strip().upper()
    if not sym:
        return []
    hours = float(lookback_hours if lookback_hours is not None else _news_max_age_hours())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("news_feed")
            .select("*")
            .eq("symbol", sym)
            .eq("exchange", ex)
            .eq("is_duplicate", False)
            .gte("published_at", cutoff)
            .order("published_at", desc=True)
            .limit(max(1, int(limit)))
            .execute()
        )
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        if "could not find the table" in msg.lower():
            logger.info("news_feed table missing; read skipped for %s", sym)
        else:
            logger.warning("news_feed read failed for %s: %s", sym, msg)
        return []
    except Exception as exc:
        logger.warning("news_feed read failed for %s: %s", sym, exc)
        return []
    rows = list(getattr(res, "data", None) or [])
    return [row for row in rows if isinstance(row, dict)]


def _load_latest_symbol_snapshot(cfg: TitanConfig, symbol: str) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    table = _symbol_snapshot_table_name()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table(table)
            .select("*")
            .eq("symbol", sym)
            .order("snapshot_at", desc=True)
            .limit(1)
            .execute()
        )
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        logger.info("Symbol news snapshot read skipped (%s): %s", table, msg)
        return None
    except Exception as exc:
        logger.info("Symbol news snapshot read skipped: %s", exc)
        return None
    rows = list(getattr(res, "data", None) or [])
    if not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _build_symbol_snapshot_payload(
    cfg: TitanConfig,
    symbol: str,
    *,
    exchange: str = "NSE",
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    items = get_recent_news_for_symbol(cfg, sym, exchange, limit=40)
    agg = aggregate_sentiment(items)
    from news_audit import extract_news_drivers

    drivers = extract_news_drivers(items)
    event_alerts = [
        {
            "event_type": str(item.get("event_type") or "general"),
            "title": str(item.get("title") or ""),
            "impact_level": str(item.get("impact_level") or "medium"),
        }
        for item in items
        if str(item.get("event_type") or "general") != "general"
    ][:5]
    recent_items = [
        {
            "title": item.get("title"),
            "source": item.get("source"),
            "published_at": item.get("published_at"),
            "sentiment": item.get("sentiment"),
            "sentiment_score": item.get("sentiment_score"),
        }
        for item in items[:10]
    ]
    now = datetime.now(timezone.utc)
    trend_score = 0.0
    if len(items) >= 2:
        first_half = items[len(items) // 2 :]
        second_half = items[: len(items) // 2]
        first_agg = aggregate_sentiment(first_half)
        second_agg = aggregate_sentiment(second_half)
        trend_score = round(float(second_agg["aggregate_score"]) - float(first_agg["aggregate_score"]), 4)
    return {
        "snapshot_at": now.isoformat(),
        "symbol": sym,
        "news_count": len(items),
        "recent_news_items": recent_items,
        "aggregate_sentiment": agg["aggregate_sentiment"],
        "aggregate_score": agg["aggregate_score"],
        "sentiment_trend": trend_score,
        "top_drivers": drivers,
        "event_alerts": event_alerts,
        "ttl_seconds": _news_snapshot_ttl_seconds(),
    }


def get_symbol_news_snapshot(
    cfg: TitanConfig,
    symbol: str,
    force_refresh: bool = False,
    *,
    exchange: str = "NSE",
) -> dict[str, Any]:
    """Get or create cached snapshot from symbol_news_snapshots."""
    sym = str(symbol or "").strip().upper()
    ttl_seconds = _news_snapshot_ttl_seconds()
    now = datetime.now(timezone.utc)
    cached = _load_latest_symbol_snapshot(cfg, sym)
    if cached and not force_refresh:
        snap_at = _to_utc_datetime(cached.get("snapshot_at"))
        age_seconds = (now - snap_at).total_seconds() if snap_at else float("inf")
        if age_seconds <= ttl_seconds:
            return {
                "source": "cached",
                "fresh": True,
                "age_seconds": round(age_seconds, 2),
                **cached,
            }
    payload = _build_symbol_snapshot_payload(cfg, sym, exchange=exchange)
    table = _symbol_snapshot_table_name()
    persisted = False
    try:
        client = create_client(cfg.supabase_url, cfg.supabase_key)
        client.table(table).insert(payload).execute()
        persisted = True
    except APIError as exc:
        api_payload = exc.args[0] if exc.args else {}
        msg = api_payload.get("message", str(exc)) if isinstance(api_payload, dict) else str(exc)
        logger.info("Symbol news snapshot persist skipped (%s): %s", table, msg)
    except Exception as exc:
        logger.info("Symbol news snapshot persist skipped: %s", exc)
    return {
        "source": "refreshed",
        "fresh": True,
        "age_seconds": 0.0,
        "persisted": persisted,
        **payload,
    }


def mark_news_as_duplicate(
    cfg: TitanConfig,
    news_id: int,
    duplicate_of_id: int,
) -> None:
    """Mark a news item as duplicate and link to original."""
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        client.table("news_feed").update(
            {
                "is_duplicate": True,
                "duplicate_of_id": int(duplicate_of_id),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", int(news_id)).execute()
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        logger.warning("mark_news_as_duplicate failed id=%s: %s", news_id, msg)
    except Exception as exc:
        logger.warning("mark_news_as_duplicate failed id=%s: %s", news_id, exc)


def cleanup_old_news(
    cfg: TitanConfig,
    older_than_hours: int = 72,
) -> dict[str, int]:
    """Prune stale rows from news_feed (cache cascades on FK)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(older_than_hours)))).isoformat()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = client.table("news_feed").delete().lt("published_at", cutoff).execute()
        rows = list(getattr(res, "data", None) or [])
        return {"deleted": len(rows)}
    except APIError as exc:
        payload = exc.args[0] if exc.args else {}
        msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
        if "could not find the table" in msg.lower():
            logger.info("news_feed table missing; cleanup skipped")
        else:
            logger.warning("news_feed cleanup failed: %s", msg)
        return {"deleted": 0}
    except Exception as exc:
        logger.warning("news_feed cleanup failed: %s", exc)
        return {"deleted": 0}
