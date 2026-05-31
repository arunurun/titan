"""Tests for news_config Supabase/env key resolution."""



from __future__ import annotations

import os

from unittest.mock import MagicMock, patch



import pytest



from config_loader import TitanConfig

from news_config import (

    FINNHUB_KEY_NAME,

    NEWSAPI_KEY_NAME,

    NEWS_RUNTIME_KEY_NAMES,

    TITAN_NEWS_FEEDS_KEY_NAME,

    TITAN_SECRETS_TABLE,

    apply_news_runtime_to_environ,

    clear_news_runtime_cache,

    get_news_api_keys,

    load_news_runtime_config,

    prepare_news_script_config,

)





@pytest.fixture

def cfg() -> TitanConfig:

    return TitanConfig(

        breeze_api_key="",

        breeze_secret="",

        breeze_session_token="",

        gemini_api_keys=[],

        supabase_url="https://example.supabase.co",

        supabase_key="service-role-key",

    )





def test_get_news_api_keys_prefers_env(monkeypatch, cfg: TitanConfig):

    monkeypatch.setenv("NEWSAPI_API_KEY", "env-news")

    monkeypatch.setenv("FINNHUB_API_KEY", "env-finn")

    keys = get_news_api_keys(cfg)

    assert keys.newsapi_api_key == "env-news"

    assert keys.finnhub_api_key == "env-finn"





def test_get_news_api_keys_supabase_fallback(monkeypatch, cfg: TitanConfig):

    clear_news_runtime_cache()

    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)



    mock_client = MagicMock()

    mock_table = MagicMock()

    mock_client.table.return_value = mock_table

    mock_table.select.return_value.in_.return_value.execute.return_value = MagicMock(

        data=[

            {"key_name": NEWSAPI_KEY_NAME, "value": "sb-news"},

            {"key_name": FINNHUB_KEY_NAME, "value": "sb-finn"},

        ]

    )



    with patch("supabase.create_client", return_value=mock_client):

        keys = get_news_api_keys(cfg)



    assert keys.newsapi_api_key == "sb-news"

    assert keys.finnhub_api_key == "sb-finn"

    mock_client.table.assert_called_with(TITAN_SECRETS_TABLE)





def test_load_news_runtime_config_all_keys(monkeypatch, cfg: TitanConfig):

    clear_news_runtime_cache()

    for name in NEWS_RUNTIME_KEY_NAMES:

        monkeypatch.delenv(name, raising=False)



    mock_client = MagicMock()

    mock_table = MagicMock()

    mock_client.table.return_value = mock_table

    mock_table.select.return_value.in_.return_value.execute.return_value = MagicMock(

        data=[

            {"key_name": NEWSAPI_KEY_NAME, "value": "n1"},

            {"key_name": TITAN_NEWS_FEEDS_KEY_NAME, "value": "https://example.com/feed.xml"},

        ]

    )



    with patch("supabase.create_client", return_value=mock_client):

        runtime = load_news_runtime_config(cfg)



    assert runtime[NEWSAPI_KEY_NAME] == "n1"

    assert runtime[TITAN_NEWS_FEEDS_KEY_NAME] == "https://example.com/feed.xml"

    assert runtime[FINNHUB_KEY_NAME] == ""





def test_apply_news_runtime_to_environ_respects_existing_env(monkeypatch, cfg: TitanConfig):
    for name in (NEWSAPI_KEY_NAME, FINNHUB_KEY_NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEWSAPI_API_KEY", "keep-me")

    applied = apply_news_runtime_to_environ(

        {NEWSAPI_KEY_NAME: "overwrite", FINNHUB_KEY_NAME: "finn-from-db"},

        cfg=cfg,

    )

    assert "NEWSAPI_API_KEY" not in applied

    assert applied[FINNHUB_KEY_NAME] == "finn-from-db"

    assert __import__("os").environ["NEWSAPI_API_KEY"] == "keep-me"


def test_prepare_news_script_config_applies_runtime_before_load_config(monkeypatch, tmp_path):
    clear_news_runtime_cache()
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "service-role-key")
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("BREEZE_API_KEY", raising=False)

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.select.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[
            {"key_name": NEWSAPI_KEY_NAME, "value": "sb-news"},
            {"key_name": FINNHUB_KEY_NAME, "value": "sb-finn"},
        ]
    )

    with patch("supabase.create_client", return_value=mock_client):
        cfg = prepare_news_script_config(env_file)

    assert cfg.supabase_url == "https://example.supabase.co"
    assert cfg.supabase_key == "service-role-key"
    assert os.environ["NEWSAPI_API_KEY"] == "sb-news"
    assert os.environ["FINNHUB_API_KEY"] == "sb-finn"


