from unittest.mock import MagicMock, patch

import pytest
from postgrest.exceptions import APIError

from config_loader import TitanConfig
from supabase_log import save_audit_log


def make_cfg():
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


@patch("supabase_log.create_client")
def test_save_audit_log_adds_ist(mock_create):
    mock_table = MagicMock()
    mock_exec = MagicMock()
    mock_exec.execute.return_value = MagicMock(data=[{"id": 1}])
    mock_table.insert.return_value = mock_exec
    mock_create.return_value.table.return_value = mock_table
    res = save_audit_log({"foo": 1}, make_cfg())
    call_kw = mock_table.insert.call_args[0][0]
    assert "recorded_at_ist" in call_kw
    assert "+05:30" in call_kw["recorded_at_ist"]
    assert "data" in res


@patch("supabase_log.create_client")
def test_save_audit_log_apierror_pgrst205_message(mock_create):
    mock_table = MagicMock()
    mock_exec = MagicMock()
    mock_exec.execute.side_effect = APIError(
        {"message": "Could not find the table 'public.audit_logs' in the schema cache", "code": "PGRST205"}
    )
    mock_table.insert.return_value = mock_exec
    mock_create.return_value.table.return_value = mock_table
    with pytest.raises(RuntimeError, match=r"\[Supabase\].*audit_logs"):
        save_audit_log({"audit": {}, "post": "x"}, make_cfg())
