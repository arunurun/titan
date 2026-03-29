from unittest.mock import MagicMock, patch

from brain import TITAN_V12_SYSTEM_INSTRUCTION, generate_titan_narrative


def test_system_instruction_has_protocol():
    assert "Titan V12.0" in TITAN_V12_SYSTEM_INSTRUCTION
    assert "Buy" in TITAN_V12_SYSTEM_INSTRUCTION or "buy" in TITAN_V12_SYSTEM_INSTRUCTION.lower()


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
