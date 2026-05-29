"""inject_breeze_session_from_supabase.py (CI helper)."""

import base64
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _fake_supabase_jwt(role: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


@pytest.fixture
def inject_mod():
    path = Path(__file__).resolve().parents[1] / "scripts" / "inject_breeze_session_from_supabase.py"
    spec = importlib.util.spec_from_file_location("inject_breeze_session_from_supabase", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_inject_writes_github_env(inject_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("BREEZE_SESSION_TOKEN", raising=False)
    gh = tmp_path / "ghenv"
    os.environ["GITHUB_ENV"] = str(gh)
    os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "sk")

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"breeze_session_token": "tok=123"}]
    )

    try:
        with patch.object(inject_mod, "create_client", return_value=mock_client):
            assert inject_mod.main() == 0
        text = gh.read_text(encoding="utf-8")
        assert "BREEZE_SESSION_TOKEN" in text
        assert "BREEZE_SESSION_TOKEN_SOURCE" in text
        assert "tok=123" in text
        mock_client.table.assert_called_once()
    finally:
        if "GITHUB_ENV" in os.environ and os.environ["GITHUB_ENV"] == str(gh):
            del os.environ["GITHUB_ENV"]


def test_inject_prefers_repository_secret_skips_supabase(inject_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "secret-token-xyz")
    gh = tmp_path / "ghenv"
    monkeypatch.setenv("GITHUB_ENV", str(gh))

    mock_create = MagicMock()
    with patch.object(inject_mod, "create_client", mock_create):
        assert inject_mod.main() == 0
    mock_create.assert_not_called()
    text = gh.read_text(encoding="utf-8")
    assert "secret-token-xyz" in text
    assert "environment:BREEZE_SESSION_TOKEN" in text


def test_jwt_role_from_supabase_key(inject_mod):
    assert inject_mod.jwt_role_from_supabase_key(_fake_supabase_jwt("anon")) == "anon"
    assert inject_mod.jwt_role_from_supabase_key(_fake_supabase_jwt("service_role")) == "service_role"


def test_inject_rejects_anon_supabase_key(inject_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("BREEZE_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "ghenv"))
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", _fake_supabase_jwt("anon"))
    with patch.object(inject_mod, "create_client") as mock_create:
        assert inject_mod.main() == 1
    mock_create.assert_not_called()


def test_inject_bootstraps_missing_row(inject_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("BREEZE_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "ghenv"))
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", _fake_supabase_jwt("service_role"))

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    # First read returns no row, then after bootstrap returns row with token.
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.side_effect = [
        MagicMock(data=[]),
        MagicMock(data=[{"breeze_session_token": "tok=abc"}]),
    ]
    with patch.object(inject_mod, "create_client", return_value=mock_client):
        assert inject_mod.main() == 0
    assert mock_table.upsert.call_count == 1
