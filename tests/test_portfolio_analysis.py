from unittest.mock import MagicMock, patch

import pytest
from config_loader import TitanConfig
from portfolio_analysis import (
    PortfolioHolding,
    _cost_basis_unreliable,
    _merge_holdings_resolving_same_ticker,
    _resolve_symbol,
    analyze_portfolio_holdings,
    collect_holdings_input,
    parse_portfolio_holdings_json,
    parse_holdings_text,
    portfolio_email_digest_plaintext,
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


def test_resolve_symbol_bhaele_maps_to_bel_not_bhel():
    uni = {"NSE": {"BEL": "BEL", "BHEL": "BHEL"}, "BSE": {}}
    sym, _ex, reason, _conf = _resolve_symbol("BHAELE", "NSE", by_exchange=uni)
    assert sym == "BEL"
    assert reason == "alias_hint"


def test_resolve_icici_pdf_contract_codes_to_nse_tickers():
    universe = {
        "NSE": {
            "NCC": "NCC",
            "CPSEETF": "CPSEETF",
            "GOLDADD": "GOLDADD",
            "JWL": "JWL",
            "HAL": "HAL",
            "PARAS": "PARAS",
            "DABUR": "DABUR",
            "IREDA": "IREDA",
            "GOLDIAM": "GOLDIAM",
            "KALYANKJIL": "KALYANKJIL",
            "UTSSAV": "UTSSAV",
        },
        "BSE": {"JYOTIRES": "JYOTIRES"},
    }
    sym, ex, reason, _ = _resolve_symbol("NAGCON", "NSE", by_exchange=universe)
    assert sym == "NCC" and reason == "icici_pdf_alias"
    sym2, ex2, _, _ = _resolve_symbol("GOLINT", "NSE", by_exchange=universe)
    assert sym2 == "GOLDIAM" and ex2 == "NSE"
    sym3, ex3, _, _ = _resolve_symbol("JYORES", "NSE", by_exchange=universe)
    assert sym3 == "JYOTIRES" and ex3 == "BSE"
    sym4, _, _, _ = _resolve_symbol("DSPGOL", "NSE", by_exchange=universe)
    assert sym4 == "GOLDADD"


def test_resolve_icici_aliases_without_market_instruments_row():
    """ETFs and other rows filtered from universe sync still resolve for portfolio."""
    empty = {"NSE": {}, "BSE": {}}
    sym, ex, reason, conf = _resolve_symbol("CPSETF", "NSE", by_exchange=empty)
    assert sym == "CPSEETF" and ex == "NSE" and reason == "icici_pdf_alias"
    assert conf >= 0.9
    sym2, ex2, reason2, _ = _resolve_symbol("DSPGOL", "NSE", by_exchange=empty)
    assert sym2 == "GOLDADD" and ex2 == "NSE" and reason2 == "icici_pdf_alias"


def test_resolve_icici_codes_avoid_fuzzy_false_positives():
    universe = {
        "NSE": {
            "KENIN": "KENIN",
            "JAYTEX": "JAYTEX",
            "BGEARRE": "BGEAR-RE",
            "NETWEB": "NETWEB",
            "KEC": "KEC",
            "SGMART": "SGMART",
            "GRSE": "GRSE",
        },
        "BSE": {"JAYTEX": "JAYTEX"},
    }
    assert _resolve_symbol("KECIN", "NSE", by_exchange=universe)[:3] == ("KEC", "NSE", "icici_pdf_alias")
    assert _resolve_symbol("JARTEX", "NSE", by_exchange=universe)[:3] == ("SGMART", "NSE", "icici_pdf_alias")
    assert _resolve_symbol("GARREA", "NSE", by_exchange=universe)[:3] == ("GRSE", "NSE", "icici_pdf_alias")
    assert _resolve_symbol("NETTEC", "NSE", by_exchange=universe)[:3] == ("NETWEB", "NSE", "icici_pdf_alias")


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


def test_merge_holdings_combine_rows_that_resolve_to_same_ticker():
    """BHAELE is Breeze/contract code for NSE BEL — duplicate lines should merge."""
    uni = {"NSE": {"BEL": "BEL", "BHEL": "BHEL"}, "BSE": {}}
    h1 = PortfolioHolding("BHAELE", "NSE", 100.0, "", avg_buy_price=100.0)
    h2 = PortfolioHolding("BEL", "NSE", 50.0, "", avg_buy_price=120.0)
    merged = _merge_holdings_resolving_same_ticker([h1, h2], by_exchange=uni)
    assert len(merged) == 1
    assert merged[0].symbol == "BHAELE"
    assert merged[0].quantity == pytest.approx(150.0)
    assert merged[0].avg_buy_price == pytest.approx((100 * 100.0 + 50 * 120.0) / 150.0)


def test_cost_basis_unreliable_heuristic():
    assert _cost_basis_unreliable(avg_buy=22.0, current_price=1788.0, pnl_pct=None) is True
    assert _cost_basis_unreliable(avg_buy=50.0, current_price=1199.0, pnl_pct=2300.0) is True
    assert _cost_basis_unreliable(avg_buy=500.0, current_price=221.33, pnl_pct=-56.0) is False


def test_resolve_symbol_subtotal_does_not_use_single_letter_prefix():
    """Regression: SUBTOTAL must not prefix-map to a single-char NSE symbol."""
    universe = {"NSE": {"S": "S"}, "BSE": {}}
    sym, ex, reason, _conf = _resolve_symbol("SUBTOTAL", "NSE", by_exchange=universe)
    assert sym == "SUBTOTAL"
    assert reason == "unresolved"


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


def test_portfolio_email_digest_includes_sizing_tape_and_multi_drivers():
    body = portfolio_email_digest_plaintext(
        source="workflow_portfolio_json",
        limitations=[],
        parsed_count=2,
        result={
            "summary": {
                "analyzed_positions": 2,
                "skipped_no_data": 0,
                "invalid_symbol_mappings": 0,
                "errors": 0,
                "portfolio_weighted_next_week_score": 50.0,
                "portfolio_weighted_intent_score": 51.0,
                "action_counts": {"exit_risk": 1, "trim": 0, "hold": 1, "buy_more": 0},
                "positions_with_cost_basis": 0,
                "portfolio_rollup_positions": 0,
            },
            "rows": [
                {
                    "status": "ok",
                    "symbol": "ALPHA",
                    "action_tag": "exit_risk",
                    "quantity": 10,
                    "current_value": 1_000_000.0,
                    "invested_value": 900_000.0,
                    "unrealized_pnl_pct": 11.0,
                    "cost_basis_unreliable": False,
                    "intent_score": 48.5,
                    "next_week_score": 41.2,
                    "return_1d_pct": -12.34,
                    "z_score": -1.25,
                    "sell_signal_risk_score": 8.75,
                    "sell_signal_reasons": ["nextWeek weak 41.2", "1d return weak -12.3%"],
                    "included_in_headline_rollup": True,
                },
                {
                    "status": "ok",
                    "symbol": "BETA",
                    "action_tag": "hold",
                    "quantity": 5,
                    "current_value": 1_000_000.0,
                    "invested_value": None,
                    "unrealized_pnl_pct": None,
                    "cost_basis_unreliable": False,
                    "intent_score": 62.0,
                    "next_week_score": 60.3,
                    "return_1d_pct": None,
                    "z_score": None,
                    "sell_signal_risk_score": 1.5,
                    "sell_signal_reasons": ["technical+risk profile stable"],
                    "included_in_headline_rollup": True,
                },
            ],
        },
    )
    assert "Risk = Titan risk points" in body
    assert "Tape |" in body
    assert "TechIntent | 1W |" in body
    assert "Curr ₹" in body
    lines = [ln for ln in body.splitlines() if ln.startswith("ALPHA ") or ln.startswith("BETA ")]
    assert len(lines) == 2
    assert "EXIT RISK" in lines[0]
    assert "1D move -12.3%" in lines[0] and "z-score -1.25" in lines[0]
    assert "8.8" in lines[0]  # risk score 8.75 → one decimal
    assert "nextWeek weak" in lines[0] and "1d return weak" in lines[0]
    assert "| 50.0% |" in lines[0] or lines[0].count("50.0") >= 1  # book weight shares


def test_portfolio_digest_inserts_gemini_brief_when_env_enabled(monkeypatch):
    monkeypatch.setenv("TITAN_PORTFOLIO_LLM_SUMMARY", "1")
    result = {
        "summary": {
            "analyzed_positions": 1,
            "skipped_no_data": 0,
            "invalid_symbol_mappings": 0,
            "errors": 0,
            "ignored_statement_lines": 0,
            "portfolio_weighted_next_week_score": 50.0,
            "portfolio_weighted_intent_score": 48.0,
            "action_counts": {"exit_risk": 1, "trim": 0, "hold": 0, "buy_more": 0},
            "portfolio_rollup_positions": 0,
            "positions_with_cost_basis": 0,
        },
        "rows": [
            {
                "status": "ok",
                "symbol": "X",
                "exchange": "NSE",
                "quantity": 100,
                "action_tag": "exit_risk",
                "current_value": 300_000.0,
                "unrealized_pnl_pct": 10.0,
                "intent_score": 40.0,
                "next_week_score": 35.0,
                "sell_signal_risk_score": 8.0,
                "return_1d_pct": -5.5,
                "z_score": -1.8,
                "sell_signal_reasons": ["nextWeek weak 35"],
                "cost_basis_unreliable": False,
                "sell_signal": "exit-risk",
            },
        ],
    }
    with patch("brain.generate_portfolio_llm_summary", return_value="- Model synthesis line.") as mock_llm:
        body = portfolio_email_digest_plaintext(
            source="unit_test",
            limitations=[],
            parsed_count=1,
            result=result,
            gemini_keys=("k",),
        )
    mock_llm.assert_called_once()
    assert "--- Portfolio brief (Gemini) ---" in body
    assert "- Model synthesis line." in body


def test_portfolio_digest_skips_gemini_when_env_off(monkeypatch):
    monkeypatch.delenv("TITAN_PORTFOLIO_LLM_SUMMARY", raising=False)
    result = {"summary": {"analyzed_positions": 0}, "rows": []}
    with patch("brain.generate_portfolio_llm_summary") as mock_llm:
        portfolio_email_digest_plaintext(
            source="unit_test",
            limitations=[],
            parsed_count=0,
            result=result,
            gemini_keys=("k",),
        )
    mock_llm.assert_not_called()
