"""Tests for CI config loader GITHUB_ENV propagation."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import load_ci_config_from_supabase as loader
from news_config import FINNHUB_KEY_NAME, NEWSAPI_KEY_NAME, SUPABASE_KEY_KEY_NAME, SUPABASE_URL_KEY_NAME


@pytest.fixture
def github_env_file(tmp_path, monkeypatch):
    path = tmp_path / "github_env"
    path.touch()
    monkeypatch.setenv("GITHUB_ENV", str(path))
    return path


def test_passthrough_supabase_keys_even_when_bootstrap_env_set(github_env_file, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "bootstrap-service-key")
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.select.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[
            {"key_name": NEWSAPI_KEY_NAME, "value": "news-from-db"},
            {"key_name": FINNHUB_KEY_NAME, "value": "finn-from-db"},
        ]
    )

    with patch("load_ci_config_from_supabase.create_client", return_value=mock_client):
        assert loader.main() == 0

    written = github_env_file.read_text(encoding="utf-8")
    assert "SUPABASE_URL=https://proj.supabase.co" in written
    assert "SUPABASE_KEY=bootstrap-service-key" in written
    assert "NEWSAPI_API_KEY=news-from-db" in written
    assert "FINNHUB_API_KEY=finn-from-db" in written


def test_missing_github_env_returns_error(monkeypatch):
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    assert loader.main() == 1
