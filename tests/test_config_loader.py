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
    assert cfg.gemini_api_keys == ("g",)
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


def test_load_config_multiple_gemini_keys(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BREEZE_API_KEY=a\nBREEZE_SECRET=b\nBREEZE_SESSION_TOKEN=c\n"
        "GEMINI_API_KEY=key1\nGEMINI_API_KEY_2=key2\n"
        "SUPABASE_URL=https://x.supabase.co\nSUPABASE_KEY=k\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BREEZE_API_KEY", raising=False)
    for k in list(os.environ.keys()):
        if k.startswith("GEMINI"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_config(env_file)
    assert cfg.gemini_api_keys == ("key1", "key2")
    assert cfg.gemini_api_key == "key1"


def test_load_config_missing_raises(monkeypatch, tmp_path):
    for k in list(os.environ.keys()):
        if k.startswith("BREEZE") or k.startswith("GEMINI") or k.startswith("SUPABASE"):
            monkeypatch.delenv(k, raising=False)
    # Explicit path: do not call load_config() with no args, or repo-root .env would load.
    missing = tmp_path / "no_env_here.env"
    assert not missing.exists()
    with pytest.raises(ValueError, match="Missing or empty required environment variable"):
        load_config(missing)


def test_load_config_reconcile_mode_skips_breeze_requirements(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=g\nSUPABASE_URL=https://x.supabase.co\nSUPABASE_KEY=k\n",
        encoding="utf-8",
    )
    for k in list(os.environ.keys()):
        if k.startswith("BREEZE"):
            monkeypatch.delenv(k, raising=False)
        if k.startswith("GEMINI"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_config(env_file, require_breeze=False)
    assert cfg.breeze_api_key == ""
    assert cfg.breeze_secret == ""
    assert cfg.breeze_session_token == ""
    assert cfg.gemini_api_keys == ("g",)
    assert cfg.supabase_key == "k"


def test_load_config_non_reconcile_still_requires_breeze(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=g\nSUPABASE_URL=https://x.supabase.co\nSUPABASE_KEY=k\n",
        encoding="utf-8",
    )
    for k in list(os.environ.keys()):
        if k.startswith("BREEZE"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="BREEZE_API_KEY"):
        load_config(env_file)
