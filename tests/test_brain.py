from unittest.mock import MagicMock, patch

import pytest
from google.genai.errors import ClientError

from brain import TITAN_V12_SYSTEM_INSTRUCTION, generate_titan_narrative


def test_system_instruction_has_protocol():
    assert "Titan V12.0" in TITAN_V12_SYSTEM_INSTRUCTION
    assert "Buy" in TITAN_V12_SYSTEM_INSTRUCTION or "buy" in TITAN_V12_SYSTEM_INSTRUCTION.lower()


@patch("brain.genai.Client")
def test_generate_rotates_on_sdk_client_error_429(mock_client_cls):
    """Real SDK errors expose .code; rotation must not rely on string parsing only."""
    err429 = ClientError(
        429,
        {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}},
        None,
    )
    ok = MagicMock(text="Index breadth remains mixed; positioning data only.")
    c1 = MagicMock()
    c1.models.generate_content.side_effect = err429
    c2 = MagicMock()
    c2.models.generate_content.return_value = ok
    mock_client_cls.side_effect = [c1, c2]
    out = generate_titan_narrative({"x": 1}, api_keys=["k1", "k2"])
    assert "positioning" in out.lower() or "index" in out.lower()
    assert mock_client_cls.call_args_list[0].kwargs.get("api_key") == "k1"
    assert mock_client_cls.call_args_list[1].kwargs.get("api_key") == "k2"


@patch("brain.genai.Client")
def test_generate_rotates_to_next_key_on_quota(mock_client_cls):
    err429 = Exception("429 RESOURCE_EXHAUSTED quota")
    ok = MagicMock(text="Index breadth remains mixed; positioning data only.")
    c1 = MagicMock()
    c1.models.generate_content.side_effect = err429
    c2 = MagicMock()
    c2.models.generate_content.return_value = ok
    mock_client_cls.side_effect = [c1, c2]
    out = generate_titan_narrative({"x": 1}, api_keys=["k1", "k2"])
    assert "positioning" in out.lower() or "index" in out.lower()
    assert mock_client_cls.call_args_list[0].kwargs.get("api_key") == "k1"
    assert mock_client_cls.call_args_list[1].kwargs.get("api_key") == "k2"


@patch("brain.time.sleep", lambda *_a, **_k: None)
@patch("brain.genai.Client")
def test_generate_retries_on_503_then_succeeds(mock_client_cls):
    err503 = Exception(
        "503 UNAVAILABLE {'error': {'message': 'high demand. Please try again later.'}}"
    )
    ok = MagicMock(text="Index breadth remains mixed; positioning data only.")
    instance = MagicMock()
    instance.models.generate_content.side_effect = [err503, ok]
    mock_client_cls.return_value = instance
    out = generate_titan_narrative({"x": 1}, api_key="dummy")
    assert "positioning" in out.lower() or "index" in out.lower()
    assert instance.models.generate_content.call_count == 2


@patch("brain.genai.Client")
def test_generate_titan_narrative_policy_pass(mock_client_cls):
    instance = MagicMock()
    mock_client_cls.return_value = instance
    instance.models.generate_content.return_value = MagicMock(
        text="Index breadth remains mixed; positioning data only."
    )
    out = generate_titan_narrative({"x": 1}, api_key="dummy")
    assert "positioning" in out.lower() or "index" in out.lower()
    mock_client_cls.assert_called_once_with(api_key="dummy")


def test_generate_skips_compliance_repair_when_env_disabled(monkeypatch):
    monkeypatch.setenv("GEMINI_COMPLIANCE_RETRY", "false")
    bad = MagicMock(text="You should buy here.")
    instance = MagicMock()
    instance.models.generate_content.return_value = bad
    with patch("brain.genai.Client", return_value=instance):
        with pytest.raises(ValueError, match="GEMINI_COMPLIANCE_RETRY=false"):
            generate_titan_narrative({"x": 1}, api_key="dummy")
    assert instance.models.generate_content.call_count == 1


@patch("brain.genai.Client")
def test_generate_titan_narrative_retry(mock_client_cls):
    bad = MagicMock(text="You should buy here.")
    good = MagicMock(text="Volatility context remains elevated; levels noted for reference only.")
    instance = MagicMock()
    instance.models.generate_content.side_effect = [bad, good]
    mock_client_cls.return_value = instance
    out = generate_titan_narrative({"x": 1}, api_key="dummy")
    assert "buy" not in out.lower()
    assert instance.models.generate_content.call_count == 2
