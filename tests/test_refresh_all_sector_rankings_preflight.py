"""Preflight token checks for weekly sector refresh."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "refresh_all_sector_rankings.py"
    spec = importlib.util.spec_from_file_location("refresh_all_sector_rankings", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_validate_token_shape_rejects_wrapped_quotes():
    mod = _load_script_module()
    with pytest.raises(ValueError, match="wrapped in quotes"):
        mod._validate_token_shape('"12345678"')


def test_validate_token_shape_rejects_newline():
    mod = _load_script_module()
    with pytest.raises(ValueError, match="newline"):
        mod._validate_token_shape("abc\n123456")


def test_validate_token_shape_accepts_valid():
    mod = _load_script_module()
    assert mod._validate_token_shape("  55142575  ") == "55142575"


def test_preflight_fails_fast_on_bad_shape(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", '"short"')
    monkeypatch.setenv("BREEZE_SESSION_TOKEN_SOURCE", "supabase:session_config(id=1)")
    with pytest.raises(RuntimeError, match="failed before sector fan-out"):
        mod._preflight_breeze_session()


def test_preflight_reports_safe_diagnostics_on_invalid_session(monkeypatch):
    mod = _load_script_module()

    class _FakeBreeze:
        def __init__(self, api_key):
            self.api_key = api_key

        def generate_session(self, api_secret, session_token):
            raise RuntimeError("Session token expired")

    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "12345678")
    monkeypatch.setenv("BREEZE_SESSION_TOKEN_SOURCE", "supabase:session_config(id=1)")
    monkeypatch.setenv("BREEZE_API_KEY", "key")
    monkeypatch.setenv("BREEZE_SECRET", "secret")
    monkeypatch.setattr(mod, "BreezeConnect", _FakeBreeze)

    with pytest.raises(RuntimeError, match="invalid/expired") as excinfo:
        mod._preflight_breeze_session()
    msg = str(excinfo.value)
    assert "source:supabase:session_config(id=1)" in msg
    assert "len:8" in msg
    assert "fp:" in msg
