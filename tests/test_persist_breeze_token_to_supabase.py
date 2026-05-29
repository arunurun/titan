"""persist_breeze_token_to_supabase input validation tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "persist_breeze_token_to_supabase.py"
    spec = importlib.util.spec_from_file_location("persist_breeze_token_to_supabase", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_persist_rejects_quote_wrapped_token_before_breeze(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("BREEZE_TOKEN_INPUT", '"12345678"')
    monkeypatch.setenv("BREEZE_API_KEY", "abc")
    monkeypatch.setenv("BREEZE_SECRET", "xyz")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "service-key")
    monkeypatch.setattr(mod, "load_dotenv", lambda override=False: None)

    mock_breeze = MagicMock()
    mock_client = MagicMock()
    monkeypatch.setattr(mod, "BreezeConnect", lambda api_key: mock_breeze)
    monkeypatch.setattr(mod, "create_client", lambda *_: mock_client)

    with pytest.raises(RuntimeError, match="wrapped in quotes"):
        mod.main()
    mock_breeze.generate_session.assert_not_called()
    mock_client.table.assert_not_called()


def test_persist_rejects_multiline_token(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("BREEZE_TOKEN_INPUT", "abc\n1234567")
    monkeypatch.setenv("BREEZE_API_KEY", "abc")
    monkeypatch.setenv("BREEZE_SECRET", "xyz")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "service-key")
    monkeypatch.setattr(mod, "load_dotenv", lambda override=False: None)

    with pytest.raises(RuntimeError, match="single-line"):
        mod.main()
