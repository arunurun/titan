"""Free-form custom equity hint → listed symbol mapping."""

from unittest.mock import MagicMock, patch

import pytest

from custom_equity_resolution import (
    is_strict_exchange_ticker,
    resolve_custom_equity_field_to_sector_instruments,
    split_custom_equity_hints,
)
from brain import resolve_equity_disambiguation_pick


def test_split_hints_respects_phrases_and_commas():
    raw = "Data Patterns Ltd\nRELIANCE, INFY"
    hints = split_custom_equity_hints(raw)
    assert hints == ["Data Patterns Ltd", "RELIANCE", "INFY"]


@pytest.fixture
def _cfg():
    from config_loader import TitanConfig

    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def test_resolve_datapattern_typo_uses_alias(monkeypatch, _cfg):
    monkeypatch.setenv("TITAN_CUSTOM_SYMBOL_LLM", "0")

    def _mini_universe(_cfg_unused):
        return {"NSE": {"DATAPATTNS": "DATAPATTNS"}, "BSE": {}}

    import portfolio_analysis

    monkeypatch.setattr(portfolio_analysis, "_load_active_symbol_universe", _mini_universe)
    inst, rows, skipped = resolve_custom_equity_field_to_sector_instruments(
        "DATAPATTERN",
        preferred_exchange="NSE",
        cfg=_cfg,
    )
    assert len(inst) == 1
    assert inst[0].symbol == "DATAPATTNS"
    assert rows[0]["via"] == "alias_hint"
    assert skipped == []


def test_resolve_exact_ticker_passthrough_without_universe_row(monkeypatch, _cfg):
    monkeypatch.setenv("TITAN_CUSTOM_SYMBOL_LLM", "0")

    def _empty_universe(_cfg_unused):
        return {"NSE": {}, "BSE": {}}

    import portfolio_analysis

    monkeypatch.setattr(portfolio_analysis, "_load_active_symbol_universe", _empty_universe)
    inst, rows, skipped = resolve_custom_equity_field_to_sector_instruments(
        "NAVINFLUOR",
        preferred_exchange="NSE",
        cfg=_cfg,
    )
    assert len(inst) == 1
    assert inst[0].symbol == "NAVINFLUOR"
    assert inst[0].exchange == "NSE"
    assert rows[0]["via"] == "exact_ticker_passthrough"
    assert skipped == []


def test_is_strict_exchange_ticker():
    assert is_strict_exchange_ticker("RELIANCE") is True
    assert is_strict_exchange_ticker("BAJAJ-AUTO") is True
    assert is_strict_exchange_ticker("Data Patterns Ltd") is False
    assert is_strict_exchange_ticker("reliance") is False


@patch("brain.genai.Client")
def test_resolve_equity_disambiguation_pick_json(mock_client_cls):
    instance = MagicMock()
    mock_client_cls.return_value = instance
    instance.models.generate_content.return_value = MagicMock(text='{"pick":1,"confidence":0.91}')
    out = resolve_equity_disambiguation_pick(
        "second one",
        [("AAA", "NSE"), ("BBB", "NSE"), ("CCC", "NSE")],
        api_key="dummy",
    )
    assert out is not None
    assert out[0] == 1
    assert out[1] == pytest.approx(0.91)
    mock_client_cls.assert_called_once_with(api_key="dummy")
