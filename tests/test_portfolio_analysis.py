from unittest.mock import MagicMock

from config_loader import TitanConfig
from portfolio_analysis import (
    PortfolioHolding,
    _resolve_symbol,
    analyze_portfolio_holdings,
    collect_holdings_input,
    parse_portfolio_holdings_json,
    parse_holdings_text,
)


def _cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def test_parse_holdings_text_basic_lines():
    raw = """
    NSE:RELIANCE,10
    INFY 5
    BSE-TCS, 3
    """
    holdings = parse_holdings_text(raw)
    assert len(holdings) == 3
    assert any(h.symbol == "RELIANCE" and h.exchange == "NSE" and h.quantity == 10 for h in holdings)
    assert any(h.symbol == "INFY" and h.exchange == "NSE" and h.quantity == 5 for h in holdings)
    assert any(h.symbol == "TCS" and h.exchange == "BSE" and h.quantity == 3 for h in holdings)


def test_collect_holdings_input_fallback_text_when_pdf_unavailable(monkeypatch):
    monkeypatch.setattr(
        "portfolio_analysis.extract_text_from_pdf",
        lambda p: ("", "PDF parsing dependency is unavailable (install pypdf or PyPDF2)."),
    )
    holdings, source, limitations = collect_holdings_input(
        pdf_path="x.pdf",
        pasted_holdings_text="NSE:SBIN, 12",
    )
    assert len(holdings) == 1
    assert source == "pasted_text"
    assert limitations


def test_analyze_portfolio_holdings_uses_equity_audit(monkeypatch):
    monkeypatch.setattr("portfolio_analysis.load_config", lambda: _cfg())
    monkeypatch.setattr("portfolio_analysis._load_supabase_breeze_token", lambda cfg: "tok")
    monkeypatch.setattr(
        "portfolio_analysis._load_active_symbol_universe",
        lambda cfg: {"NSE": {"HAL": "HAL"}, "BSE": {}},
    )
    monkeypatch.setattr("breeze_client.create_breeze_session", lambda cfg: MagicMock())

    def fake_audit(cfg, breeze, inst, **kwargs):
        return (
            {
                "symbol": inst.symbol,
                    "effective_intent_score": 72.0,
                    "next_week_score": 72.0,
                "z_score": 1.1,
                "return_1d_pct": 0.8,
                "absorption_ratio": 1.2,
                "close_last": 120.0,
                "sell_signal": "hold",
                "sell_signal_risk_score": 21.0,
                "sell_signal_reasons": ["trend ok"],
            },
            "",
        )

    monkeypatch.setattr("portfolio_analysis.build_equity_live_audit", fake_audit)
    result = analyze_portfolio_holdings(
        [PortfolioHolding(symbol="HAL", exchange="NSE", quantity=10, source_line="HAL 10", avg_buy_price=100)]
    )
    assert result["summary"]["analyzed_positions"] == 1
    assert result["summary"]["portfolio_weighted_next_week_score"] == 72.0
    assert result["top_candidates"][0]["symbol"] == "HAL"
    assert result["top_candidates"][0]["action_tag"] == "buy_more"
    assert result["summary"]["positions_with_cost_basis"] == 1


def test_resolve_symbol_numeric_suffix_and_alias():
    universe = {
        "NSE": {
            "DATAPATTNS": "DATAPATTNS",
            "ASTRAMICRO": "ASTRAMICRO",
            "JYOTIRES": "JYOTIRES",
        },
        "BSE": {},
    }
    s1 = _resolve_symbol("DATPAT156212", "NSE", by_exchange=universe)
    s2 = _resolve_symbol("ASTMIC31501", "NSE", by_exchange=universe)
    s3 = _resolve_symbol("JYORES5101", "NSE", by_exchange=universe)
    assert s1[0] == "DATAPATTNS"
    assert s2[0] == "ASTRAMICRO"
    assert s3[0] == "JYOTIRES"


def test_parse_portfolio_holdings_json_basic():
    payload = """
    [
      {"symbol": "NSE:HAL", "quantity": 10, "avg_buy_price": 120.5},
      {"symbol": "INFY", "qty": 5}
    ]
    """
    out = parse_portfolio_holdings_json(payload, default_exchange="NSE")
    assert len(out) == 2
    assert out[0].symbol == "HAL" and out[0].exchange == "NSE"
    assert out[0].avg_buy_price == 120.5
    assert out[1].symbol == "INFY"
