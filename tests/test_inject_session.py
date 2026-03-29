"""inject_breeze_session_from_supabase.py (CI helper)."""

import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def inject_mod():
    path = Path(__file__).resolve().parents[1] / "scripts" / "inject_breeze_session_from_supabase.py"
    spec = importlib.util.spec_from_file_location("inject_breeze_session_from_supabase", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_inject_writes_github_env(inject_mod, tmp_path):
    gh = tmp_path / "ghenv"
    os.environ["GITHUB_ENV"] = str(gh)
    os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "sk")

    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"breeze_session_token": "tok=123"}]
    )

    try:
        with patch.object(inject_mod, "create_client", return_value=mock_client):
            assert inject_mod.main() == 0
        text = gh.read_text(encoding="utf-8")
        assert "BREEZE_SESSION_TOKEN" in text
        assert "tok=123" in text
    finally:
        if "GITHUB_ENV" in os.environ and os.environ["GITHUB_ENV"] == str(gh):
            del os.environ["GITHUB_ENV"]
