import os

import pytest

from config_loader import load_breeze_config, load_config


def test_load_config_reads_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BREEZE_API_KEY=a\nBREEZE_SECRET=b\nBREEZE_SESSION_TOKEN=c\n"
        "GEMINI_API_KEY=g\nSUPABASE_URL=https://x.supabase.co\nSUPABASE_KEY=k\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BREEZE_API_KEY", raising=False)
    cfg = load_config(env_file)
    assert cfg.breeze_api_key == "a"
    assert cfg.supabase_url.startswith("https://")


def test_load_breeze_config_only_breeze_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BREEZE_API_KEY=a\nBREEZE_SECRET=b\nBREEZE_SESSION_TOKEN=c\n",
        encoding="utf-8",
    )
    cfg = load_breeze_config(env_file)
    assert cfg.breeze_api_key == "a"
    assert cfg.breeze_session_token == "c"


def test_load_config_missing_raises(monkeypatch):
    for k in list(os.environ.keys()):
        if k.startswith("BREEZE") or k.startswith("GEMINI") or k.startswith("SUPABASE"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError):
        load_config()
