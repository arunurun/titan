"""Load news/CI runtime config from env with optional Supabase fallback."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import TitanConfig

logger = logging.getLogger(__name__)

TITAN_SECRETS_TABLE = "titan_secrets"
NEWSAPI_KEY_NAME = "NEWSAPI_API_KEY"
FINNHUB_KEY_NAME = "FINNHUB_API_KEY"
SUPABASE_URL_KEY_NAME = "SUPABASE_URL"
SUPABASE_KEY_KEY_NAME = "SUPABASE_KEY"
TITAN_NEWS_FEEDS_KEY_NAME = "TITAN_NEWS_FEEDS"

NEWS_RUNTIME_KEY_NAMES: tuple[str, ...] = (
    NEWSAPI_KEY_NAME,
    FINNHUB_KEY_NAME,
    SUPABASE_URL_KEY_NAME,
    SUPABASE_KEY_KEY_NAME,
    TITAN_NEWS_FEEDS_KEY_NAME,
)

_CACHE_TTL_SECONDS = 60.0
_runtime_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


@dataclass(frozen=True)
class NewsApiKeys:
    newsapi_api_key: str
    finnhub_api_key: str


def _env_value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _load_keys_from_supabase(cfg: TitanConfig, missing: list[str]) -> dict[str, str]:
    if not missing or not cfg.supabase_url or not cfg.supabase_key:
        return {}
    try:
        from supabase import create_client

        client = create_client(cfg.supabase_url, cfg.supabase_key)
        res = (
            client.table(TITAN_SECRETS_TABLE)
            .select("key_name,value")
            .in_("key_name", missing)
            .execute()
        )
    except Exception as exc:
        logger.warning(
            "Failed to load config from Supabase table %s: %s",
            TITAN_SECRETS_TABLE,
            exc,
        )
        return {}
    out: dict[str, str] = {}
    for row in getattr(res, "data", None) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("key_name") or "").strip()
        if not name:
            continue
        out[name] = str(row.get("value") or "").strip()
    return out


def _cached_supabase_map(cfg_url: str, cfg_key: str, missing: tuple[str, ...]) -> dict[str, str]:
    now = time.monotonic()
    cache_key = (cfg_url, cfg_key)
    entry = _runtime_cache.get(cache_key)
    if entry is not None and (now - entry[0]) < _CACHE_TTL_SECONDS:
        cached = entry[1]
        return {k: cached.get(k, "") for k in missing}

    from config_loader import TitanConfig

    cfg = TitanConfig(
        breeze_api_key="",
        breeze_secret="",
        breeze_session_token="",
        gemini_api_keys=[],
        supabase_url=cfg_url,
        supabase_key=cfg_key,
    )
    loaded = _load_keys_from_supabase(cfg, list(missing))
    _runtime_cache[cache_key] = (now, dict(loaded))
    return loaded


def clear_news_runtime_cache() -> None:
    """Clear in-process Supabase config cache (for tests)."""
    _runtime_cache.clear()


def load_news_runtime_config(cfg: TitanConfig | None = None) -> dict[str, str]:
    """
    Resolve news/CI keys: process env first, then Supabase ``titan_secrets`` rows.

    Returns a dict with all ``NEWS_RUNTIME_KEY_NAMES`` (values may be empty).
    """
    out: dict[str, str] = {name: _env_value(name) for name in NEWS_RUNTIME_KEY_NAMES}
    missing = [name for name, val in out.items() if not val]
    if not missing:
        return out
    if cfg is None or not cfg.supabase_url or not cfg.supabase_key:
        return out

    loaded = _cached_supabase_map(cfg.supabase_url, cfg.supabase_key, tuple(missing))
    for name in missing:
        if not out[name]:
            out[name] = loaded.get(name, "")
    return out


def apply_news_runtime_to_environ(
    runtime: dict[str, str] | None = None,
    *,
    cfg: TitanConfig | None = None,
) -> dict[str, str]:
    """Set missing env vars from runtime config (env always wins). Returns applied mapping."""
    resolved = runtime if runtime is not None else load_news_runtime_config(cfg)
    applied: dict[str, str] = {}
    for name in NEWS_RUNTIME_KEY_NAMES:
        if _env_value(name):
            continue
        value = (resolved.get(name) or "").strip()
        if value:
            os.environ[name] = value
            applied[name] = value
    return applied


def prepare_news_script_config(
    env_path: str | Path | None = None,
):
    """Load TitanConfig for news scripts after applying titan_secrets runtime keys."""
    from config_loader import TitanConfig, load_config

    apply_news_runtime_to_environ()

    url = _env_value(SUPABASE_URL_KEY_NAME)
    key = _env_value(SUPABASE_KEY_KEY_NAME)
    if url and key:
        bootstrap = TitanConfig(
            breeze_api_key="",
            breeze_secret="",
            breeze_session_token="",
            gemini_api_keys=(),
            supabase_url=url,
            supabase_key=key,
        )
        apply_news_runtime_to_environ(load_news_runtime_config(bootstrap))

    if not _env_value(SUPABASE_URL_KEY_NAME) or not _env_value(SUPABASE_KEY_KEY_NAME):
        raise ValueError(
            "Missing SUPABASE_URL or SUPABASE_KEY. In CI, run load_ci_config_from_supabase.py "
            "first (bootstrap secrets) or set both in the environment."
        )
    return load_config(env_path, require_breeze=False, require_gemini=False)


def get_news_api_keys(cfg: TitanConfig | None = None) -> NewsApiKeys:
    """Resolve NEWSAPI/FINNHUB keys: process env first, then Supabase titan_secrets."""
    runtime = load_news_runtime_config(cfg)
    newsapi = runtime.get(NEWSAPI_KEY_NAME, "")
    finnhub = runtime.get(FINNHUB_KEY_NAME, "")
    if newsapi and finnhub:
        return NewsApiKeys(newsapi_api_key=newsapi, finnhub_api_key=finnhub)
    return NewsApiKeys(newsapi_api_key=newsapi, finnhub_api_key=finnhub)


def get_titan_news_feeds(cfg: TitanConfig | None = None) -> str:
    """Comma-separated RSS feed URLs from env or Supabase."""
    return load_news_runtime_config(cfg).get(TITAN_NEWS_FEEDS_KEY_NAME, "")
