"""Token validator script should send action-required email on invalid token."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "validate_breeze_token_from_supabase.py"
    spec = importlib.util.spec_from_file_location("validate_breeze_token_from_supabase", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_validator_sends_email_on_invalid_token(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("BREEZE_API_KEY", "abc")
    monkeypatch.setenv("BREEZE_SECRET", "xyz")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "service-key")
    monkeypatch.setenv("TOKEN_UPDATE_URL", "https://example.com/update")
    monkeypatch.setattr(mod, "load_dotenv", lambda override=False: None)

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = SimpleNamespace(data=[{"breeze_session_token": "bad-token"}])
    mock_client = MagicMock()
    mock_client.table.return_value = mock_table
    monkeypatch.setattr(mod, "create_client", lambda *_: mock_client)

    mock_breeze = MagicMock()
    mock_breeze.generate_session.side_effect = RuntimeError("Session token expired")
    monkeypatch.setattr(mod, "BreezeConnect", lambda api_key: mock_breeze)

    mock_notify = MagicMock()
    monkeypatch.setattr(mod, "send_action_required_email", mock_notify)

    code = mod.main()
    assert code == 2
    mock_notify.assert_called_once()
    assert "invalid or expired" in mock_notify.call_args[0][0].lower()
    assert mock_notify.call_args[1]["action_label"] == "Login to Breeze"
    assert "api.icicidirect.com/apiuser/login?api_key=abc" in mock_notify.call_args[1]["action_url"]
