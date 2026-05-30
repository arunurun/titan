"""News quality checks and correlation with technical audit signals."""

from __future__ import annotations

import logging
import os
from typing import Any

from config_loader import TitanConfig
from news_sentiment import aggregate_sentiment
from news_store import get_recent_news_for_symbol

logger = logging.getLogger(__name__)

_IMPACT_WEIGHTS = {"high": 1.0, "medium": 0.6, "low": 0.3}
_NEWS_DRIVER_LIMIT_DEFAULT = 3


def _news_driver_limit() -> int:
    raw = (str(os.environ.get("TITAN_NEWS_DRIVER_LIMIT", "")) or "").strip()
    if not raw:
        return _NEWS_DRIVER_LIMIT_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _NEWS_DRIVER_LIMIT_DEFAULT


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def validate_news_payload(audit: dict[str, Any]) -> tuple[bool, list[str]]:
    """Quality checks on enriched audit dict before Gemini / persist."""
    issues: list[str] = []
    if not isinstance(audit, dict):
        return False, ["audit_not_dict"]
    recent_news = audit.get("recent_news")
    if recent_news is not None and not isinstance(recent_news, list):
        issues.append("recent_news_not_list")
    score = audit.get("news_sentiment_score")
    if score is not None:
        try:
            score_f = float(score)
            if score_f < -1.0 or score_f > 1.0:
                issues.append("news_sentiment_score_out_of_range")
        except (TypeError, ValueError):
            issues.append("news_sentiment_score_invalid")
    aggregate = audit.get("news_sentiment_aggregate")
    if aggregate is not None and str(aggregate) not in ("positive", "negative", "neutral", "mixed"):
        issues.append("news_sentiment_aggregate_invalid")
    news_count = audit.get("news_count")
    if news_count is not None:
        try:
            if int(news_count) < 0:
                issues.append("news_count_negative")
        except (TypeError, ValueError):
            issues.append("news_count_invalid")
    if isinstance(recent_news, list):
        for idx, item in enumerate(recent_news):
            if not isinstance(item, dict):
                issues.append(f"recent_news[{idx}]_not_dict")
                continue
            if not str(item.get("title") or "").strip():
                issues.append(f"recent_news[{idx}]_missing_title")
    return len(issues) == 0, issues


def compute_news_sentiment_trend(
    cfg: TitanConfig,
    symbol: str,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Compute sentiment momentum over a rolling window."""
    sym = str(symbol or "").strip().upper()
    items = get_recent_news_for_symbol(cfg, sym, lookback_hours=window_hours, limit=50)
    if not items:
        return {"trend": "flat", "trend_score": 0.0, "item_count": 0}
    midpoint = max(1, len(items) // 2)
    older = items[midpoint:]
    newer = items[:midpoint]
    older_agg = aggregate_sentiment(older)
    newer_agg = aggregate_sentiment(newer)
    trend_score = round(float(newer_agg["aggregate_score"]) - float(older_agg["aggregate_score"]), 4)
    if trend_score >= 0.12:
        trend = "strengthening"
    elif trend_score <= -0.12:
        trend = "weakening"
    else:
        trend = "flat"
    return {
        "trend": trend,
        "trend_score": trend_score,
        "item_count": len(items),
        "older_score": older_agg["aggregate_score"],
        "newer_score": newer_agg["aggregate_score"],
    }


def correlate_news_with_price_move(
    cfg: TitanConfig,
    symbol: str,
    audit_data: dict[str, Any],
) -> dict[str, Any]:
    """Compare sentiment direction with 1-day price change from audit."""
    _ = cfg
    _ = symbol
    sentiment_score = _safe_float(audit_data.get("news_sentiment_score"))
    if sentiment_score == 0.0 and audit_data.get("recent_news"):
        agg = aggregate_sentiment(list(audit_data.get("recent_news") or []))
        sentiment_score = _safe_float(agg.get("aggregate_score"))
    return_pct = _safe_float(audit_data.get("return_1d_pct"))
    if return_pct == 0.0:
        return_pct = _safe_float(audit_data.get("return_1d"))
    sentiment_dir = 0
    if sentiment_score >= 0.15:
        sentiment_dir = 1
    elif sentiment_score <= -0.15:
        sentiment_dir = -1
    price_dir = 0
    if return_pct >= 0.5:
        price_dir = 1
    elif return_pct <= -0.5:
        price_dir = -1
    if sentiment_dir == 0 or price_dir == 0:
        aligned = True
        contradiction_strength = 0.0
        possible_reason = "insufficient_signal"
    else:
        aligned = sentiment_dir == price_dir
        contradiction_strength = round(min(1.0, abs(sentiment_score) * abs(return_pct) / 10.0), 4)
        possible_reason = "" if aligned else _contradiction_reason(sentiment_score, return_pct)
    return {
        "aligned": aligned,
        "contradiction_strength": 0.0 if aligned else contradiction_strength,
        "possible_reason": possible_reason,
        "sentiment_score": sentiment_score,
        "return_1d_pct": return_pct,
    }


def _contradiction_reason(sentiment_score: float, return_pct: float) -> str:
    if sentiment_score > 0 and return_pct < 0:
        return "positive_headlines_with_negative_price"
    if sentiment_score < 0 and return_pct > 0:
        return "negative_headlines_with_positive_price"
    return "sentiment_price_divergence"


def extract_news_drivers(
    items: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank news by impact_level × relevance_score."""
    cap = limit if limit is not None else _news_driver_limit()
    ranked: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        impact = _IMPACT_WEIGHTS.get(str(item.get("impact_level") or "medium").lower(), 0.6)
        relevance = max(0.0, _safe_float(item.get("relevance_score")) or 0.5)
        contribution = round(impact * relevance, 4)
        ranked.append(
            {
                "headline": title,
                "source": str(item.get("source") or "unknown").strip() or "unknown",
                "published_at": str(item.get("published_at") or "").strip(),
                "impact_contribution": contribution,
                "impact_level": str(item.get("impact_level") or "medium"),
                "relevance_score": relevance,
                "sentiment": item.get("sentiment"),
                "sentiment_score": item.get("sentiment_score"),
                "url": str(item.get("url") or "").strip(),
            }
        )
    ranked.sort(key=lambda row: _safe_float(row.get("impact_contribution")), reverse=True)
    return ranked[: max(1, int(cap))]
