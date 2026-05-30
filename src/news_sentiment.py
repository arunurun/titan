"""Sentiment analysis utilities for financial news."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_VADER_READY = False
_EVENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "earnings": ("earnings", "results", "q1", "q2", "q3", "q4", "profit", "revenue", "ebitda", "guidance"),
    "acquisition": ("acquisition", "acquire", "merger", "takeover", "buyout", "m&a"),
    "regulatory": ("regulatory", "sebi", "rbi", "compliance", "probe", "investigation", "approval", "ban"),
    "dividend": ("dividend", "interim dividend", "final dividend", "payout", "buyback"),
}
_TICKER_RE = re.compile(r"\b[A-Z]{2,12}\b")
_COMPANY_SUFFIXES = (" ltd", " limited", " corp", " corporation", " inc", " plc", " industries")
_POSITIVE_TERMS = frozenset(
    {"surge", "beat", "beating", "growth", "wins", "approval", "record", "upgrade", "profit", "jumps", "jump"}
)
_NEGATIVE_TERMS = frozenset({"fall", "drop", "cuts", "downgrade", "probe", "ban", "loss", "miss", "decline", "crash", "violation"})


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _normalize_text(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", t).strip()


def sentiment_model_name() -> str:
    raw = (str(os.environ.get("TITAN_SENTIMENT_MODEL", "")) or "").strip().lower()
    if raw in ("finbert", "transformers", "bert"):
        return "finbert"
    return "vader"


def _ensure_vader() -> Any:
    global _VADER_READY
    try:
        import nltk
        from nltk.sentiment import SentimentIntensityAnalyzer
    except ImportError as exc:
        raise RuntimeError("nltk is required for VADER sentiment. Install with: pip install nltk") from exc
    if not _VADER_READY:
        try:
            nltk.data.find("sentiment/vader_lexicon.zip")
        except LookupError:
            logger.info("Downloading NLTK vader_lexicon...")
            nltk.download("vader_lexicon", quiet=True)
        _VADER_READY = True
    return SentimentIntensityAnalyzer()


def _label_from_score(score: float) -> str:
    if score >= 0.25:
        return "positive"
    if score <= -0.25:
        return "negative"
    if abs(score) < 0.08:
        return "neutral"
    return "mixed"


def _financial_lexicon_adjustment(text: str) -> float:
    t = _normalize_text(text).lower()
    if not t:
        return 0.0
    pos_hits = sum(1 for term in _POSITIVE_TERMS if term in t)
    neg_hits = sum(1 for term in _NEGATIVE_TERMS if term in t)
    raw = (pos_hits - neg_hits) / max(2.0, pos_hits + neg_hits + 1.0)
    return round(_clamp(raw, -1.0, 1.0), 4)


def compute_sentiment_vader(text: str) -> dict[str, Any]:
    """VADER sentiment (fast, financial-aware heuristic lexicon)."""
    txt = _normalize_text(text)
    if not txt:
        return {"sentiment": "neutral", "score": 0.0, "confidence": 0.0, "model": "vader"}
    analyzer = _ensure_vader()
    scores = analyzer.polarity_scores(txt)
    compound = float(scores.get("compound") or 0.0)
    fin_adj = _financial_lexicon_adjustment(txt)
    blended = round(_clamp((compound * 0.5) + (fin_adj * 0.5), -1.0, 1.0), 4)
    return {
        "sentiment": _label_from_score(blended),
        "score": blended,
        "confidence": round(_clamp(abs(blended), 0.0, 1.0), 4),
        "model": "vader",
        "raw": scores,
    }


def compute_sentiment_transformers(
    text: str,
    model_name: str = "ProsusAI/finbert",
) -> dict[str, Any]:
    """FinBERT / transformer sentiment (optional heavy dependency)."""
    txt = _normalize_text(text)
    if not txt:
        return {
            "sentiment": "neutral",
            "score": 0.0,
            "confidence": 0.0,
            "model": model_name,
            "computation_time_ms": 0.0,
        }
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError(
            "transformers/torch not installed. Install optional deps: pip install -r requirements-news.txt"
        ) from exc
    start = time.perf_counter()
    try:
        clf = pipeline("sentiment-analysis", model=model_name, truncation=True, max_length=512)
        result = clf(txt[:2000])[0]
    except Exception as exc:
        raise RuntimeError(f"Transformer sentiment failed for model={model_name}: {exc}") from exc
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    label = str(result.get("label") or "neutral").lower()
    conf = float(result.get("score") or 0.0)
    if "pos" in label:
        sentiment = "positive"
        score = conf
    elif "neg" in label:
        sentiment = "negative"
        score = -conf
    else:
        sentiment = "neutral"
        score = 0.0
    return {
        "sentiment": sentiment,
        "score": round(_clamp(score, -1.0, 1.0), 4),
        "confidence": round(_clamp(conf, 0.0, 1.0), 4),
        "model": model_name,
        "computation_time_ms": round(elapsed_ms, 2),
    }


def compute_sentiment(text: str) -> dict[str, Any]:
    """Route to configured sentiment model via TITAN_SENTIMENT_MODEL."""
    if sentiment_model_name() == "finbert":
        try:
            return compute_sentiment_transformers(text)
        except RuntimeError as exc:
            logger.warning("FinBERT unavailable (%s); falling back to VADER", exc)
    return compute_sentiment_vader(text)


def aggregate_sentiment(
    items: list[dict[str, Any]],
    weight_by_relevance: bool = True,
) -> dict[str, Any]:
    """Weighted or equal average of sentiment scores."""
    if not items:
        return {"aggregate_sentiment": "neutral", "aggregate_score": 0.0, "item_count": 0}
    total_weight = 0.0
    weighted_sum = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = float(item.get("sentiment_score") or 0.0)
        weight = float(item.get("relevance_score") or 1.0) if weight_by_relevance else 1.0
        weight = max(0.05, weight)
        weighted_sum += score * weight
        total_weight += weight
    aggregate_score = 0.0 if total_weight <= 0.0 else weighted_sum / total_weight
    aggregate_score = round(_clamp(aggregate_score, -1.0, 1.0), 4)
    return {
        "aggregate_sentiment": _label_from_score(aggregate_score),
        "aggregate_score": aggregate_score,
        "item_count": len(items),
    }


def extract_event_type(title: str, text: str = "") -> str:
    """Classify news into event buckets."""
    blob = _normalize_text(f"{title} {text}").lower()
    if not blob:
        return "general"
    best = "general"
    best_hits = 0
    for event_type, terms in _EVENT_PATTERNS.items():
        hits = sum(1 for term in terms if term in blob)
        if hits > best_hits:
            best_hits = hits
            best = event_type
    return best


def extract_company_entities(text: str) -> list[str]:
    """Extract likely company names / tickers from news body."""
    txt = _normalize_text(text)
    if not txt:
        return []
    entities: list[str] = []
    seen: set[str] = set()
    for match in _TICKER_RE.findall(txt.upper()):
        if match in seen:
            continue
        seen.add(match)
        entities.append(match)
    lower = txt.lower()
    for suffix in _COMPANY_SUFFIXES:
        idx = 0
        while True:
            pos = lower.find(suffix, idx)
            if pos < 0:
                break
            start = lower.rfind(" ", 0, pos)
            name = txt[start + 1 : pos + len(suffix)].strip(" ,:-")
            if len(name) >= 3:
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    entities.append(name.title())
            idx = pos + len(suffix)
    return entities[:20]
