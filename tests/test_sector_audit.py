"""Sector equity audit (cash metrics, mocked Breeze/Gemini)."""

import math
import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config_loader import TitanConfig
from sector_registry import SectorInstrument


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


@pytest.fixture(autouse=True)
def _patch_sector_audit_load_config(monkeypatch):
    monkeypatch.setattr("sector_audit.load_config", lambda **kwargs: make_cfg())


@pytest.fixture(autouse=True)
def _default_empty_news_feed_cache(monkeypatch, request):
    """Correlation tests default to empty news_feed unless a test opts into cache."""
    if request.node.get_closest_marker("uses_news_cache"):
        return
    monkeypatch.setattr("news_store.get_recent_news_for_symbol", lambda *a, **k: [])
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "empty_feed",
        },
    )


def test_symbol_digest_default_is_short_block(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.8,
        "volume_participation_ratio": 1.304,
        "volume_participation_for_scoring": 1.81,
        "return_1d_pct": -4.28,
        "ema_200_distance_pct": 47.29,
        "atr_14_pct": 3.42,
        "adx_14": 22.6,
        "adx_plus_di_14": 29.1,
        "adx_minus_di_14": 18.4,
        "breakout_20d_distance_pct_to_high": -0.8,
        "breakout_20d_distance_pct_above_low": 9.4,
        "atr_14_over_atr_63": 1.12,
        "cmf_20": 0.11,
        "next_day_score": 49.77,
        "next_week_score": 51.84,
        "sell_signal": "trim",
        "sell_signal_reasons": ["nextWeek soft 51.84", "intent cooling 50.00"],
        "fundamental_status": "unavailable",
        "fundamental_score": float("nan"),
        "fundamental_reasons": [],
        "hypothesis_support": "technical_only",
        "high_volume_down_day_proxy": False,
        "panic_absorption_proxy": False,
        "trap_exit_proxy": False,
        "cluster_guardrail_applied": True,
        "macro_guardrail_applied": False,
        "event_risk_soon": False,
        "rows": 37,
        "exchange_used": "NSE",
        "exchange_fallback_used": False,
        "prediction_breakdown": {
            "week": {
                "tech_composite_term": 0.0,
                "ema_term": 4.3,
                "ret1d_term": -1.93,
                "atr_penalty": 0.54,
            },
            "day": {},
            "penalties": [],
        },
        "sector_pctile_effective_intent": 62.0,
        "rel_return_5d_vs_nifty_pct": 0.35,
        "rel_return_20d_vs_nifty_pct": -0.12,
    }
    result = {"symbol": "WELCORP", "exchange": "NSE", "audit": audit}
    text = _format_symbol_metrics_line(result)
    assert "techScore" not in text
    assert "WELCORP (NSE)" in text
    assert "HOLD" in text
    assert "intent cooling" in text.lower()
    assert "1W outlook:" in text
    assert "neutral band" in text.lower()
    assert "Regime: Buy trend" in text
    assert "ADX 22.60 (weak)" in text
    assert "+DI 29.10 > −DI 18.40" in text
    assert "20D range: -0.80% to high · +9.40% above low · near-high" in text
    assert "ADX strength: <20 sideways · 20–24 weak · ≥25 strong" in text
    assert "vs 3M: 1.12x high" in text
    assert "CMF (EOD): 0.110 accumulation" in text
    assert "CMF: >0.05 acc · −0.05 to 0.05 neutral · <−0.05 dist" in text
    assert "▸ Trend Regime (14D)" in text
    assert "▸ 20D Money Flow" in text
    assert "▸ 1D / Tape" in text
    assert "▸ Model outlook" in text
    assert "Percentile: ≥67 leader · 34–66 average · ≤33 laggard" in text
    assert "1D outlook:" in text
    assert "EMA200: +47.29% · hot" in text
    assert "ATR14: 3.42% moderate" in text
    assert "ATR14: <2 calm · 2–4 moderate · >4 elevated" in text
    assert "1D move (EOD):" in text
    assert "VPR: ≥1.5 high · 1.0–1.49 above-avg · 0.7–0.99 below · <0.7 thin" in text
    assert "Horizon bands: ≥70 strong · 55–69 constructive" in text
    assert "Intent bands: ≥70 high-long · 55–69 mod-long" in text
    assert "Why this action:" in text
    assert "   • nextWeek soft 51.84" in text
    assert "\n" in text
    assert "Model read:" in text
    assert "Confidence: ≥70 high · 55–69 medium · <55 low" in text
    assert "🟢⬆ Regime: Buy trend" in text
    assert "🔴⬇ 1D move (EOD): -4.28%" in text
    assert "Intent percentile: 62.00 average" in text
    headline_end = text.index("\n")
    model_idx = text.lower().index("▸ model outlook")
    assert model_idx > headline_end
    assert text.lower().index("1w outlook:") > model_idx
    assert text.lower().index("technical intent:") > model_idx


@pytest.mark.parametrize(
    "ema_dist,expected_icon",
    [
        (3.0, "🟢⬆"),
        (5.0, "🟢⬆"),
        (10.0, "🟢⬆"),
        (12.0, "🟡➡"),
        (15.0, "🟡➡"),
        (18.0, "🟠➡"),
        (25.0, "🟠➡"),
        (30.0, "🔴⬇"),
        (69.0, "🔴⬇"),
        (-3.0, "🟡➡"),
        (-7.0, "🔴⬇"),
        (float("nan"), "🟡➡"),
    ],
)
def test_ema200_distance_icon_thresholds(ema_dist, expected_icon):
    from sector_audit import _ema200_distance_icon

    assert _ema200_distance_icon(ema_dist) == expected_icon


def test_symbol_digest_default_shows_neutral_na_for_missing_new_metrics(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.8,
        "volume_participation_ratio": 1.1,
        "return_1d_pct": -0.5,
        "atr_14_pct": 2.0,
        "next_week_score": 51.0,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend strength"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "Regime: Sideways · ADX n/a (n/a) · DI unavailable" in text
    assert "20D range: n/a% to high · n/a% above low · n/a" in text
    assert "vs 3M: n/ax n/a" in text
    assert "CMF (EOD): n/a n/a · as of n/a" in text
    assert "CMF: >0.05 acc · −0.05 to 0.05 neutral · <−0.05 dist" in text
    assert "VPR (EOD): 1.10x above-avg · as of n/a" in text
    assert "News correlation unavailable: correlation metadata missing" in text


def test_symbol_digest_includes_global_news_correlation_line(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 58.0,
        "z_score": 1.1,
        "volume_participation_ratio": 1.2,
        "return_1d_pct": 0.9,
        "atr_14_pct": 2.1,
        "next_week_score": 60.2,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "news_correlation": {
            "driver": "AI chip investment surge (FeedX)",
            "affected_metric": "momentum 5D",
            "affected_theme": "ai",
            "direction": "tailwind",
            "confidence": 0.73,
            "driver_source": "stock",
            "stock_news_fetched_count": 2,
            "stock_news_coverage": "fetched",
            "evidence": {
                "net_news_impact_score": 0.2142,
                "net_news_impact_direction": "tailwind",
                "top_headlines": {
                    "global": [
                        {
                            "headline": "Chip capex rises globally",
                            "source": "Reuters",
                            "published_at": "2026-05-30T08:00:00+00:00",
                            "impact_contribution_score": 0.1822,
                        }
                    ],
                    "local": [],
                    "stock": [],
                },
            },
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "Stock news: AI chip investment surge (FeedX)" in text
    assert "tailwind" in text
    assert "momentum 5D" in text
    assert "fetched=2" in text
    assert "News evidence: impact 0.2142 (tailwind)" in text
    assert "Reuters" in text


def test_apply_global_news_correlation_uses_explicit_fallback_when_sector_missing(monkeypatch):
    from sector_audit import _apply_global_news_correlation

    snapshot = {
        "source": "cached",
        "news_items": [
            {
                "title": "Global chip policy update impacts markets",
                "source": "Reuters",
                "published_at": "2026-05-30T08:00:00+00:00",
            }
        ],
        "sector_scores": {
            "defence": {
                "score": -0.3,
                "confidence": 0.82,
                "matched_items": 1,
                "drivers_top": [
                    {
                        "driver": "Defence spending delayed",
                        "title": "Defence spending delayed",
                        "source": "Reuters",
                        "published_at": "2026-05-30T07:50:00+00:00",
                        "contribution": -0.42,
                        "confidence": 0.81,
                    }
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "empty_feed",
        },
    )
    ok_results = [{"audit": {"prediction_breakdown": {"week": {"ema_term": 1.3}}}}]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="ai", ok_results=ok_results)
    corr = ok_results[0]["audit"]["news_correlation"]
    assert meta["applied"] is True
    assert "fallback_label" in corr
    assert "sector_specific_match_missing_using_global_market_driver" == corr["fallback_label"]
    assert corr["driver_source"] == "macro"
    assert corr["stock_news_fetched_count"] == 0
    assert corr["stock_news_coverage"] == "not_covered"
    assert corr["used_macro_fallback"] is True
    assert corr["confidence"] < 0.5


def test_symbol_digest_news_line_present_with_fallback_label(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 48.0,
        "z_score": -0.5,
        "volume_participation_ratio": 0.9,
        "return_1d_pct": -0.2,
        "atr_14_pct": 2.4,
        "next_week_score": 47.1,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "news_correlation": {
            "driver": "Global chip policy update (Reuters)",
            "affected_metric": "trend",
            "affected_theme": "ai",
            "direction": "neutral",
            "confidence": 0.32,
            "driver_source": "macro",
            "stock_news_fetched_count": 0,
            "stock_news_coverage": "fetched",
            "fallback_label": "sector_specific_match_missing_using_global_market_driver",
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "Macro:" in text
    assert "fallback=using_global_market_driver" in text


def test_apply_global_news_correlation_sets_line_for_all_audits_when_snapshot_empty(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_correlation_line

    snapshot = {"source": "unavailable", "news_items": [], "sector_scores": {}}
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "unavailable",
        },
    )
    ok_results = [
        {"symbol": "HAL", "exchange": "NSE", "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ret1d_term": 0.7}}}},
        {"symbol": "BEL", "exchange": "NSE", "audit": {"symbol": "BEL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": -0.5}}}},
    ]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="unknown_sector", ok_results=ok_results)
    assert meta["applied"] is True
    for row in ok_results:
        line = _news_correlation_line(row["audit"])
        assert "Macro fallback relation:" in line
        assert "fallback=sector_specific_match_missing_no_market_driver" in line


def test_apply_global_news_correlation_macro_snapshot_survives_missing_stock_helpers(monkeypatch):
    from sector_audit import _apply_global_news_correlation

    snapshot = {
        "source": "cached",
        "fresh": True,
        "age_minutes": 12.0,
        "sector_scores": {
            "defence": {
                "score": 0.19,
                "confidence": 0.67,
                "drivers_top": [
                    {
                        "title": "Global defence exports rise",
                        "source": "Reuters",
                        "published_at": "2026-05-30T08:30:00+00:00",
                        "contribution": 0.17,
                    }
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.delattr("sector_priority.fetch_stock_news_for_symbol", raising=False)
    monkeypatch.delattr("sector_priority.correlate_stock_news_with_macro", raising=False)
    ok_results = [
        {
            "symbol": "HAL",
            "exchange": "NSE",
            "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.2}}},
        }
    ]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    assert meta["applied"] is True
    assert meta["applied_count"] == 1
    assert meta["snapshot"]["source"] == "cached"
    assert meta["snapshot_available"] is True
    corr = ok_results[0]["audit"]["news_correlation"]
    assert corr["fallback_label"] == "macro_only_fallback"
    assert corr["stock_news_coverage"] == "helper_unavailable"
    assert corr["driver_source"] == "macro"
    assert isinstance(corr.get("evidence"), dict)


def test_symbol_digest_verbose_restores_legacy_line(monkeypatch):
    monkeypatch.setenv("TITAN_DIGEST_VERBOSE_SYMBOLS", "1")
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.8,
        "volume_participation_ratio": 1.304,
        "volume_participation_for_scoring": 1.81,
        "return_1d_pct": -4.28,
        "ema_200_distance_pct": 47.29,
        "atr_14_pct": 3.42,
        "next_day_score": 49.77,
        "next_week_score": 51.84,
        "sell_signal": "trim",
        "sell_signal_reasons": ["nextWeek soft 51.84"],
        "fundamental_status": "unavailable",
        "fundamental_score": float("nan"),
        "fundamental_reasons": [],
        "hypothesis_support": "technical_only",
        "rows": 37,
        "exchange_used": "NSE",
        "exchange_fallback_used": False,
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line({"symbol": "WELCORP", "exchange": "NSE", "audit": audit})
    assert "techIntent" in text
    assert "score-input" in text


def test_build_equity_live_audit_skips_narrative(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    mock_gen = MagicMock(return_value="should not run")
    monkeypatch.setattr("brain.generate_titan_narrative", mock_gen)

    breeze = MagicMock()
    inst = SectorInstrument("HAL", "NSE")
    audit, post = build_equity_live_audit(
        make_cfg(), breeze, inst, sector_id="defence", with_narrative=False
    )
    assert post == ""
    assert audit["symbol"] == "HAL"
    mock_gen.assert_not_called()


def test_build_equity_live_audit_fno_populates_options(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )

    opt_payload = {
        "underlying": "RELIANCE",
        "call_oi": 1000.0,
        "put_oi": 1200.0,
        "call_chain_df": pd.DataFrame({"strike": [100.0, 105.0], "oi": [100.0, 500.0]}),
        "put_chain_df": pd.DataFrame({"strike": [95.0, 100.0], "oi": [200.0, 800.0]}),
        "chain_df": pd.DataFrame({"strike": [100.0], "oi": [600.0]}),
        "expiry_date": "2026-06-24T06:00:00.000Z",
    }
    monkeypatch.setattr(
        "breeze_client.fetch_option_metrics_with_expiry_fallback",
        lambda *a, **k: opt_payload,
    )

    breeze = MagicMock()
    inst = SectorInstrument("RELIANCE", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), breeze, inst, sector_id="energy")
    assert audit["option_chain_unavailable"] is False
    assert audit["put_oi_wall_strike"] == 100.0
    assert audit["call_oi_wall_strike"] == 105.0
    assert "pcr" in audit


def test_build_equity_live_audit_success(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )

    breeze = MagicMock()
    inst = SectorInstrument("HAL", "NSE")
    audit, post = build_equity_live_audit(make_cfg(), breeze, inst, sector_id="defence")
    assert post == "Post body"
    assert audit["symbol"] == "HAL"
    assert audit["sector"] == "defence"
    assert audit["option_chain_unavailable"] is True
    assert audit["option_chain_fetch_attempted"] is True
    assert audit["option_chain_not_fno"] is False
    assert "return_1d_pct" in audit
    assert "ema_200_distance_pct" in audit
    assert "atr_14_pct" in audit
    assert "adx_14" in audit
    assert "breakout_20d_distance_pct_to_high" in audit
    assert "atr_14_over_atr_63" in audit
    assert "cmf_20" in audit
    assert "cmf_20_delta" in audit
    assert "effective_intent_score" in audit
    assert audit.get("z_score_blend") == "20d_only"
    assert "high_volume_down_day_proxy" in audit


def test_build_equity_live_audit_non_fno_flags_not_fno(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )

    breeze = MagicMock()
    inst = SectorInstrument("DYNAMATECH", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), breeze, inst, sector_id="defence")
    assert audit["option_chain_unavailable"] is True
    assert audit["option_chain_not_fno"] is True
    assert audit["option_chain_fetch_attempted"] is False


def test_format_symbol_options_context_non_fno_message():
    from sector_audit import _format_symbol_options_context_block

    lines = _format_symbol_options_context_block(
        {"option_chain_unavailable": True, "option_chain_not_fno": True}
    )
    assert lines[0] == "▸ Options context"
    assert "not in F&O universe" in lines[1]


def test_format_symbol_options_context_fetch_failed_message():
    from sector_audit import _format_symbol_options_context_block

    lines = _format_symbol_options_context_block(
        {
            "option_chain_unavailable": True,
            "option_chain_not_fno": False,
            "option_chain_fetch_attempted": True,
            "option_chain_unavailable_reason": "zero open interest",
        }
    )
    assert lines[0] == "▸ Options context"
    assert "chain unavailable (zero open interest)" in lines[1]


def test_format_symbol_options_context_success_with_mock_chain():
    from sector_audit import _format_symbol_options_context_block

    lines = _format_symbol_options_context_block(
        {
            "option_chain_unavailable": False,
            "pcr": 1.15,
            "put_oi_wall_strike": 4200.0,
            "call_oi_wall_strike": 4400.0,
            "options_expiry": "2026-06-30T06:00:00.000Z",
            "close_last": 4350.0,
            "spot_vs_put_wall_pct": 3.57,
            "spot_vs_call_wall_pct": -1.14,
        }
    )
    assert lines[0] == "▸ Options context"
    assert "PCR 1.15" in lines[1]
    assert "4400" in lines[1]
    assert any("Spot" in line for line in lines)


def test_format_sector_options_context_block_available():
    from sector_audit import _format_sector_options_context_block

    lines = _format_sector_options_context_block(
        {
            "sector_option_chain_unavailable": False,
            "sector_options_underlying": "NIFTY",
            "sector_pcr": 0.76,
            "sector_put_wall_strike": 22500.0,
            "sector_call_wall_strike": 24000.0,
            "sector_options_expiry": "2026-06-09T06:00:00.000Z",
            "sector_index_spot": 23366.7,
            "sector_spot_vs_put_wall_pct": 3.85,
            "sector_spot_vs_call_wall_pct": -2.64,
        }
    )
    assert lines[0] == "▸ Sector options context"
    assert "NIFTY PCR 0.76" in lines[1]
    assert "Index spot" in lines[2]


def test_build_equity_live_audit_cmf20_delta_is_numeric(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(35)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [1_000_000 + i * 1000 for i in range(35)]
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows, "volume": volumes})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )
    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), MagicMock(), inst, sector_id="defence")
    delta = audit.get("cmf_20_delta")
    assert isinstance(delta, dict)
    assert isinstance(delta.get("previous_value"), float)
    assert isinstance(delta.get("current_value"), float)
    assert isinstance(delta.get("absolute_change"), float)
    rel = delta.get("relative_change_percent")
    assert isinstance(rel, (float, int)) or rel is None
    assert isinstance(delta.get("interpretation"), str) and delta["interpretation"]


def test_apply_global_news_correlation_attaches_explicit_evidence(monkeypatch):
    from sector_audit import _apply_global_news_correlation
    from sector_registry import SectorInstrument

    snapshot = {
        "source": "cached",
        "fresh": True,
        "sector_scores": {
            "defence": {
                "score": 0.12,
                "confidence": 0.82,
                "drivers_top": [
                    {
                        "title": "Global defense spending accelerates",
                        "source": "Reuters",
                        "published_at": "2026-05-30T09:10:00+00:00",
                        "contribution": 0.0842,
                    },
                    {
                        "title": "India defence stocks rally on order pipeline",
                        "source": "Moneycontrol",
                        "published_at": "2026-05-30T07:05:00+00:00",
                        "contribution": 0.0611,
                    },
                    {
                        "title": "HAL shares jump after earnings beat",
                        "source": "Economic Times",
                        "published_at": "2026-05-30T06:00:00+00:00",
                        "contribution": 0.0528,
                    },
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda cfg: snapshot)
    monkeypatch.setattr("sector_priority.load_priority_instruments", lambda _cfg, sector_key, top_n=None: [SectorInstrument("HAL", "NSE")])
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "empty_feed",
        },
    )
    ok_results = [{"audit": {"symbol": "HAL", "prediction_breakdown": {"week": {"ema_term": 2.0}}}}]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    assert meta["applied"] is True
    corr = ok_results[0]["audit"]["news_correlation"]
    assert isinstance(corr, dict)
    evidence = corr.get("evidence")
    assert isinstance(evidence, dict)
    assert isinstance(evidence.get("net_news_impact_score"), float)
    assert evidence.get("net_news_impact_direction") in ("tailwind", "headwind", "neutral")
    buckets = evidence.get("top_headlines")
    assert isinstance(buckets, dict)
    assert set(["global", "local", "stock"]).issubset(set(buckets.keys()))
    flat = [row for k in ("global", "local", "market", "stock") for row in buckets.get(k, [])]
    assert flat
    assert all(isinstance(x.get("impact_contribution_score"), float) for x in flat)
    assert all(str(x.get("source") or "").strip() for x in flat)
    assert all("published_at" in x for x in flat)


def test_apply_global_news_correlation_produces_symbol_specific_lines(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_correlation_line

    snapshot = {
        "source": "cached",
        "fresh": True,
        "news_items": [],
        "sector_scores": {
            "defence": {
                "score": 0.08,
                "confidence": 0.7,
                "drivers_top": [
                    {
                        "title": "Defence order visibility improves globally",
                        "source": "Reuters",
                        "published_at": "2026-05-30T09:10:00+00:00",
                        "contribution": 0.06,
                    }
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)

    def _fake_stock_news(_cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None):
        headline = "HAL wins engine contract" if symbol == "HAL" else "BEL secures radar export order"
        return {
            "symbol": symbol,
            "exchange": exchange,
            "items": [
                {
                    "title": headline,
                    "summary": "Order pipeline expands",
                    "source": "ET Markets",
                    "url": f"https://x/{symbol.lower()}",
                    "published_at": "2026-05-30T10:00:00+00:00",
                }
            ],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "",
        }

    monkeypatch.setattr("sector_priority.fetch_stock_news_for_symbol", _fake_stock_news)
    ok_results = [
        {"symbol": "HAL", "exchange": "NSE", "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.2}}}},
        {"symbol": "BEL", "exchange": "NSE", "audit": {"symbol": "BEL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.1}}}},
    ]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    assert meta["applied"] is True
    hal_line = _news_correlation_line(ok_results[0]["audit"])
    bel_line = _news_correlation_line(ok_results[1]["audit"])
    assert "Stock news relation:" in hal_line
    assert "Stock news relation:" in bel_line
    assert "HAL wins engine contract" in hal_line
    assert "BEL secures radar export order" in bel_line
    assert hal_line != bel_line


def test_apply_global_news_correlation_records_alias_fallback(monkeypatch):
    from sector_audit import _apply_global_news_correlation
    from sector_registry import SectorInstrument

    snapshot = {
        "source": "cached",
        "fresh": True,
        "news_items": [],
        "sector_scores": {
            "defence": {
                "score": 0.06,
                "confidence": 0.68,
                "drivers_top": [],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr("sector_priority.load_priority_instruments", lambda _cfg, sector_key, top_n=None: [SectorInstrument("HAL", "NSE")])
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [
                {
                    "title": "Hindustan Aeronautics signs maintenance pact",
                    "summary": "",
                    "source": "Moneycontrol",
                    "url": "https://x/hal-maint",
                    "published_at": "2026-05-30T09:00:00+00:00",
                }
            ],
            "query_used": "Hindustan Aeronautics",
            "alias_used": "Hindustan Aeronautics",
            "fallback_used": True,
            "error": "",
        },
    )
    ok_results = [{"symbol": "HAL", "exchange": "NSE", "audit": {"symbol": "HAL", "exchange": "NSE"}}]
    _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    stock_meta = ok_results[0]["audit"]["news_correlation"]["stock_news"]
    assert stock_meta["alias_fallback_used"] is True
    assert stock_meta["alias_used"] == "Hindustan Aeronautics"


def test_news_evidence_line_shows_stock_fetch_error(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 48.0,
        "z_score": -0.5,
        "volume_participation_ratio": 0.9,
        "return_1d_pct": -0.2,
        "atr_14_pct": 2.4,
        "next_week_score": 47.1,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "news_correlation": {
            "driver": "Global chip policy update (Reuters)",
            "affected_metric": "trend",
            "affected_theme": "defence",
            "direction": "neutral",
            "confidence": 0.32,
            "driver_source": "macro",
            "stock_news_fetched_count": 0,
            "stock_news_coverage": "empty:empty_feed",
            "fallback_label": "sector_specific_match_missing_using_global_market_driver",
            "stock_news": {
                "fetched_count": 0,
                "query_used": "HAL",
                "alias_used": "",
                "alias_fallback_used": False,
                "fetch_error": "empty_feed",
            },
            "evidence": {
                "net_news_impact_score": 0.01,
                "net_news_impact_direction": "neutral",
                "stock_fetch_error": "empty_feed",
                "top_headlines": {
                    "global": [
                        {
                            "headline": "Defence exports rise",
                            "source": "Reuters",
                            "published_at": "2026-05-30T08:00:00+00:00",
                            "impact_contribution_score": 0.12,
                        }
                    ],
                    "local": [],
                    "market": [],
                    "stock": [],
                },
            },
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "stock=none (fetch_error=empty_feed; query=HAL)" in text
    assert "stock_fetch_error=empty_feed" in text


def test_apply_global_news_correlation_empty_feed_records_reason(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_evidence_line

    snapshot = {
        "source": "cached",
        "sector_scores": {
            "defence": {
                "score": 0.1,
                "confidence": 0.7,
                "drivers_top": [
                    {
                        "title": "Defence demand rises",
                        "source": "Reuters",
                        "published_at": "2026-05-30T08:00:00+00:00",
                        "contribution": 0.08,
                    }
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr(
        "sector_priority.fetch_stock_news_for_symbol",
        lambda _cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None: {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "empty_feed",
        },
    )
    ok_results = [
        {
            "symbol": "HAL",
            "exchange": "NSE",
            "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.0}}},
        }
    ]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    corr = ok_results[0]["audit"]["news_correlation"]
    assert meta["applied"] is True
    assert corr["stock_news_coverage"] == "empty:empty_feed"
    assert corr["stock_news"]["fetch_error"] == "empty_feed"
    assert corr["evidence"]["stock_fetch_error"] == "empty_feed"
    evidence_line = _news_evidence_line(ok_results[0]["audit"])
    assert "stock=none (fetch_error=empty_feed; query=HAL)" in evidence_line


def test_apply_global_news_correlation_missing_helpers_shows_reason_in_evidence(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_evidence_line

    snapshot = {
        "source": "cached",
        "sector_scores": {
            "defence": {
                "score": 0.19,
                "confidence": 0.67,
                "drivers_top": [
                    {
                        "title": "Global defence exports rise",
                        "source": "Reuters",
                        "published_at": "2026-05-30T08:30:00+00:00",
                        "contribution": 0.17,
                    }
                ],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.delattr("sector_priority.fetch_stock_news_for_symbol", raising=False)
    monkeypatch.delattr("sector_priority.correlate_stock_news_with_macro", raising=False)
    ok_results = [
        {
            "symbol": "HAL",
            "exchange": "NSE",
            "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.2}}},
        }
    ]
    _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    corr = ok_results[0]["audit"]["news_correlation"]
    assert corr["stock_news_coverage"] == "helper_unavailable"
    evidence_line = _news_evidence_line(ok_results[0]["audit"])
    assert "stock=none (fetch_error=helper_unavailable)" in evidence_line


@pytest.mark.uses_news_cache
def test_apply_global_news_correlation_uses_cache_before_live_fetch(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_correlation_line

    snapshot = {
        "source": "cached",
        "fresh": True,
        "sector_scores": {
            "defence": {
                "score": 0.08,
                "confidence": 0.7,
                "drivers_top": [],
            }
        },
    }
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    live_called: list[str] = []

    def _live_fetch(_cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None):
        live_called.append(symbol)
        raise AssertionError("live fetch should not run when cache has items")

    monkeypatch.setattr("sector_priority.fetch_stock_news_for_symbol", _live_fetch)
    monkeypatch.setattr(
        "news_store.get_recent_news_for_symbol",
        lambda _cfg, symbol, exchange, lookback_hours=None, limit=20: [
            {
                "title": f"{symbol} cached headline",
                "summary": "from news_feed",
                "source": "Moneycontrol",
                "url": f"https://x/{symbol.lower()}",
                "published_at": "2026-05-30T10:00:00+00:00",
            }
        ],
    )
    ok_results = [
        {
            "symbol": "HAL",
            "exchange": "NSE",
            "audit": {"symbol": "HAL", "exchange": "NSE", "prediction_breakdown": {"week": {"ema_term": 1.2}}},
        }
    ]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    assert meta["applied"] is True
    assert live_called == []
    corr = ok_results[0]["audit"]["news_correlation"]
    assert corr["stock_news_coverage"] == "cached"
    assert corr["driver_source"] == "stock"
    line = _news_correlation_line(ok_results[0]["audit"])
    assert "HAL cached headline" in line


@pytest.mark.uses_news_cache
def test_apply_global_news_correlation_skips_live_fetch_beyond_top_n(monkeypatch):
    from sector_audit import _apply_global_news_correlation

    snapshot = {
        "source": "cached",
        "sector_scores": {
            "defence": {"score": 0.05, "confidence": 0.6, "drivers_top": []},
        },
    }
    monkeypatch.setenv("TITAN_STOCK_NEWS_COVERAGE_TOP_N", "1")
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr("news_store.get_recent_news_for_symbol", lambda *a, **k: [])
    live_calls: list[str] = []

    def _live_fetch(_cfg, symbol, exchange, timeout_seconds=10.0, now_utc=None):
        live_calls.append(symbol)
        return {
            "symbol": symbol,
            "exchange": exchange,
            "items": [],
            "query_used": symbol,
            "alias_used": "",
            "fallback_used": False,
            "error": "empty_feed",
        }

    monkeypatch.setattr("sector_priority.fetch_stock_news_for_symbol", _live_fetch)
    ok_results = [
        {"symbol": "BEL", "exchange": "NSE", "audit": {"symbol": "BEL", "exchange": "NSE"}},
        {"symbol": "HAL", "exchange": "NSE", "audit": {"symbol": "HAL", "exchange": "NSE"}},
    ]
    _apply_global_news_correlation(make_cfg(), sector_id="defence", ok_results=ok_results)
    assert live_calls == ["BEL"]
    assert ok_results[1]["audit"]["news_correlation"]["stock_news_coverage"] == "empty:cache_miss_live_skipped"


def test_build_equity_live_audit_records_exchange_fallback_metadata(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    df.attrs["exchange_requested"] = "NSE"
    df.attrs["exchange_used"] = "BSE"
    df.attrs["exchange_fallback_used"] = True
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )

    breeze = MagicMock()
    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), breeze, inst, sector_id="defence")
    assert audit["exchange"] == "NSE"
    assert audit["exchange_used"] == "BSE"
    assert audit["exchange_fallback_used"] is True


def test_blend_equity_z_score_short_series_is_fast_only():
    from sector_audit import _blend_equity_z_score

    s = pd.Series([100.0 + i * 0.05 for i in range(30)])
    z, z_fast, z_slow, note = _blend_equity_z_score(s)
    assert note == "20d_only"
    assert z_slow is None
    assert z == z_fast


def test_blend_equity_z_score_blends_when_enough_history():
    from sector_audit import _blend_equity_z_score

    s = pd.Series([100.0 + i * 0.02 + 0.1 * math.sin(i / 5.0) for i in range(50)])
    z, z_fast, z_slow, note = _blend_equity_z_score(s)
    assert z_slow is not None
    assert "0.55*" in note
    assert z == round(0.55 * z_fast + 0.45 * z_slow, 4)


def test_build_equity_live_audit_event_flags(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "high": closes, "low": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )
    breeze = MagicMock()
    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(
        make_cfg(),
        breeze,
        inst,
        sector_id="defence",
        event_snapshot={"events": [{"symbol": "HAL", "date": "2026-04-12", "type": "earnings"}]},
    )
    assert "event_risk_present" in audit
    assert "event_risk_soon" in audit


def test_build_equity_live_audit_empty_raises(monkeypatch):
    from sector_audit import build_equity_live_audit

    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: pd.DataFrame())

    breeze = MagicMock()
    inst = SectorInstrument("X", "NSE")
    with pytest.raises(RuntimeError, match="No rows"):
        build_equity_live_audit(
            make_cfg(), breeze, inst, sector_id="defence", strict_data=True
        )


def test_build_equity_live_audit_empty_not_strict_returns_skip(monkeypatch):
    from sector_audit import build_equity_live_audit

    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: pd.DataFrame())

    breeze = MagicMock()
    inst = SectorInstrument("X", "NSE")
    audit, post = build_equity_live_audit(
        make_cfg(), breeze, inst, sector_id="defence", strict_data=False
    )
    assert post == ""
    assert audit.get("skipped_no_data") is True
    assert audit["rows"] == 0
    assert audit["symbol"] == "X"


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_calls_workers(mock_load, mock_process, mock_email):
    from sector_audit import run_sector_live

    mock_load.return_value = [
        SectorInstrument("A", "NSE"),
        SectorInstrument("B", "NSE"),
    ]
    mock_process.side_effect = [
        {"ok": True, "symbol": "A", "exchange": "NSE", "post": "pa", "error": None},
        {"ok": True, "symbol": "B", "exchange": "NSE", "post": "pb", "error": None},
    ]

    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        run_sector_live("defence", max_workers=2, digest=False)

    assert mock_process.call_count == 2
    mock_email.assert_called_once()


@patch("email_notify.send_success_post_email")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_all_fail_raises(mock_load, mock_email):
    from sector_audit import run_sector_live

    mock_load.return_value = [SectorInstrument("Z", "NSE")]

    def boom(*a, **k):
        raise RuntimeError("[Breeze] fail")

    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("sector_audit._process_one", side_effect=boom):
            with pytest.raises(RuntimeError, match="All 1 instruments failed"):
                run_sector_live("defence", max_workers=1, digest=False)

    mock_email.assert_not_called()


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_digest_action_summary_counts_accumulate_separately(mock_load, mock_metrics, mock_email):
    from sector_audit import run_sector_live

    mock_load.return_value = [
        SectorInstrument("ACC", "NSE"),
        SectorInstrument("HLD", "NSE"),
        SectorInstrument("TRM", "NSE"),
        SectorInstrument("EXT", "NSE"),
    ]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "ACC",
            "exchange": "NSE",
            "audit": {"symbol": "ACC", "z_score": 0.5, "intent_score": 0.5, "rows": 30, "sell_signal": "accumulate"},
            "error": None,
        },
        {
            "ok": True,
            "symbol": "HLD",
            "exchange": "NSE",
            "audit": {"symbol": "HLD", "z_score": 0.2, "intent_score": 0.4, "rows": 30, "sell_signal": "hold"},
            "error": None,
        },
        {
            "ok": True,
            "symbol": "TRM",
            "exchange": "NSE",
            "audit": {"symbol": "TRM", "z_score": -0.5, "intent_score": 0.3, "rows": 30, "sell_signal": "trim"},
            "error": None,
        },
        {
            "ok": True,
            "symbol": "EXT",
            "exchange": "NSE",
            "audit": {"symbol": "EXT", "z_score": -1.0, "intent_score": 0.1, "rows": 30, "sell_signal": "exit-risk"},
            "error": None,
        },
    ]
    def _preserve_preset_sell_signal(audit):
        presets = {
            "ACC": "accumulate",
            "HLD": "hold",
            "TRM": "trim",
            "EXT": "exit-risk",
        }
        preset = presets.get(str(audit.get("symbol") or "").upper(), "hold")
        return preset, 3.0, ["preset signal"]

    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("sector_audit.load_config", return_value=make_cfg()):
            with patch("sector_audit._derive_sell_signal", side_effect=_preserve_preset_sell_signal):
                with patch("brain.generate_sector_digest_narrative", return_value="Action mix post"):
                    with patch("supabase_log.save_audit_log"):
                        with patch(
                            "analysis_store.persist_sector_run_analytics",
                            return_value={"persisted": True, "run_id": "test-action-summary"},
                        ):
                            with patch("analysis_store.update_sector_period_rollups"):
                                with patch(
                                    "analysis_store.build_comparison_payload",
                                    return_value={"enabled": False},
                                ):
                                    with patch(
                                        "analysis_store.persist_llm_digest_memory",
                                        return_value={"persisted": True},
                                    ):
                                        run_sector_live("defence", max_workers=4, digest=True)

    body = mock_email.call_args[0][0]
    assert "--- Action summary ---" in body
    assert "BUY: 0 | ACCUMULATE: 1 | HOLD: 1 | TRIM: 1 | EXIT RISK: 1" in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_digest_one_gemini_call(mock_load, mock_metrics, mock_email):
    from sector_audit import run_sector_live

    mock_load.return_value = [
        SectorInstrument("A", "NSE"),
        SectorInstrument("B", "NSE"),
    ]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "A",
            "exchange": "NSE",
            "audit": {"symbol": "A", "z_score": 1.0, "intent_score": 0.5, "absorption_ratio": 0.3, "rows": 30},
            "error": None,
        },
        {
            "ok": True,
            "symbol": "B",
            "exchange": "NSE",
            "audit": {"symbol": "B", "z_score": -0.5, "intent_score": 0.2, "absorption_ratio": 0.1, "rows": 25},
            "error": None,
        },
    ]
    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch(
            "brain.generate_sector_digest_narrative", return_value="One combined post"
        ) as mock_digest:
            with patch("supabase_log.save_audit_log") as mock_save:
                with patch(
                    "analysis_store.persist_sector_run_analytics",
                    return_value={"persisted": True, "run_id": "test-run-digest"},
                ):
                    with patch("analysis_store.update_sector_period_rollups"):
                        with patch(
                            "analysis_store.build_comparison_payload",
                            return_value={"enabled": False},
                        ):
                            with patch(
                                "analysis_store.persist_llm_digest_memory",
                                return_value={"persisted": True},
                            ):
                                run_sector_live("defence", max_workers=2, digest=True)

    mock_digest.assert_called_once()
    assert mock_save.call_count == 2
    mock_email.assert_called_once()
    body = mock_email.call_args[0][0]
    assert "digest mode: 1 Gemini call" in body
    assert "--- Decision-first top section ---" in body
    assert "One combined post" not in body
    assert "Per-symbol metrics" in body
    assert "Risk overlays" not in body
    assert "--- Executive snapshot (verbose) ---" not in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_metals_mining_includes_pm_macro(
    mock_load, mock_metrics, mock_email, monkeypatch
):
    from pathlib import Path

    from sector_audit import run_sector_live

    fixture = Path(__file__).parent / "fixtures" / "pm_macro_series.csv"
    monkeypatch.setenv("TITAN_PM_MACRO_EMAIL", "1")
    monkeypatch.setenv("TITAN_PM_LIVE_FETCH", "0")
    monkeypatch.setenv("TITAN_PM_MACRO_CSV", str(fixture))
    mock_load.return_value = [SectorInstrument("WELCORP", "NSE")]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "WELCORP",
            "exchange": "NSE",
            "audit": {
                "symbol": "WELCORP",
                "z_score": 1.0,
                "intent_score": 0.5,
                "absorption_ratio": 0.3,
                "rows": 30,
                "ohlc_bar_as_of_date": "2026-06-05",
            },
            "error": None,
        }
    ]
    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("brain.generate_sector_digest_narrative", return_value="Metals narrative"):
            with patch("supabase_log.save_audit_log"):
                with patch(
                    "analysis_store.persist_sector_run_analytics",
                    return_value={"persisted": True, "run_id": "test-metals-pm"},
                ):
                    with patch("analysis_store.update_sector_period_rollups"):
                        with patch(
                            "analysis_store.build_comparison_payload",
                            return_value={"enabled": False},
                        ):
                            with patch(
                                "analysis_store.persist_llm_digest_memory",
                                return_value={"persisted": True},
                            ):
                                run_sector_live("metals_mining", max_workers=1, digest=True)
    body = mock_email.call_args[0][0]
    assert "--- Precious metals macro ---" in body
    assert "▸ Recommended allocation" in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_digest_verbose_sections_enabled(
    mock_load, mock_metrics, mock_email, monkeypatch
):
    from sector_audit import run_sector_live

    monkeypatch.setenv("TITAN_DIGEST_VERBOSE_SECTIONS", "1")
    mock_load.return_value = [SectorInstrument("A", "NSE")]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "A",
            "exchange": "NSE",
            "audit": {
                "symbol": "A",
                "z_score": 1.0,
                "intent_score": 62.0,
                "effective_intent_score": 60.0,
                "absorption_ratio": 1.2,
                "return_1d_pct": 0.6,
                "rows": 30,
            },
            "error": None,
        }
    ]
    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("brain.generate_sector_digest_narrative", return_value="One combined post"):
            with patch("supabase_log.save_audit_log"):
                with patch(
                    "analysis_store.persist_sector_run_analytics",
                    return_value={"persisted": True, "run_id": "test-verbose-sections"},
                ):
                    with patch("analysis_store.update_sector_period_rollups"):
                        with patch(
                            "analysis_store.build_comparison_payload",
                            return_value={"enabled": False},
                        ):
                            with patch(
                                "analysis_store.persist_llm_digest_memory",
                                return_value={"persisted": True},
                            ):
                                run_sector_live("defence", max_workers=1, digest=True)
    body = mock_email.call_args[0][0]
    assert "--- Executive snapshot (verbose) ---" in body
    assert "Risk overlays" in body


def test_symbol_digest_explicit_news_unavailable_message(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 52.0,
        "z_score": 0.6,
        "volume_participation_ratio": 1.1,
        "return_1d_pct": 0.2,
        "atr_14_pct": 1.9,
        "next_week_score": 54.3,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "news_correlation": {
            "available": False,
            "unavailable_reason": "global_news_snapshot_unavailable",
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "News: unavailable (global_news_snapshot_unavailable)" in text


def test_symbol_digest_fundamentals_unavailable_hint(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 52.0,
        "z_score": 0.6,
        "volume_participation_ratio": 1.1,
        "return_1d_pct": 0.2,
        "atr_14_pct": 1.9,
        "next_week_score": 54.3,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "fundamental_status": "unavailable",
        "fundamental_score": float("nan"),
        "fundamental_reasons": [],
        "hypothesis_support": "technical_only",
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line({"symbol": "LAURUSLABS", "exchange": "NSE", "audit": audit})
    assert "Fundamentals: unavailable (run ingest_fundamentals)" in text


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_macro_guardrail_applied(mock_load, mock_metrics, mock_email, monkeypatch):
    from sector_audit import run_sector_live

    monkeypatch.setenv("TITAN_DIGEST_VERBOSE_SECTIONS", "1")
    monkeypatch.setattr(
        "sector_priority.resolve_global_news_snapshot",
        lambda _cfg: {"source": "unavailable", "sector_scores": {}},
    )
    mock_load.return_value = [
        SectorInstrument("A", "NSE"),
        SectorInstrument("B", "NSE"),
    ]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "A",
            "exchange": "NSE",
            "audit": {
                "symbol": "A",
                "z_score": 1.2,
                "intent_score": 62.0,
                "effective_intent_score": 62.0,
                "absorption_ratio": 1.1,
                "return_1d_pct": -0.3,
                "rows": 30,
            },
            "error": None,
        },
        {
            "ok": True,
            "symbol": "B",
            "exchange": "NSE",
            "audit": {
                "symbol": "B",
                "z_score": 0.8,
                "intent_score": 58.0,
                "effective_intent_score": 58.0,
                "absorption_ratio": 1.0,
                "return_1d_pct": -0.2,
                "rows": 30,
            },
            "error": None,
        },
    ]
    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("brain.generate_sector_digest_narrative", return_value="One combined post"):
            with patch("supabase_log.save_audit_log"):
                with patch(
                    "analysis_store.persist_sector_run_analytics",
                    return_value={"persisted": True, "run_id": "test-run-macro"},
                ):
                    with patch("analysis_store.update_sector_period_rollups"):
                        with patch(
                            "analysis_store.build_comparison_payload",
                            return_value={"enabled": False},
                        ):
                            with patch(
                                "analysis_store.persist_llm_digest_memory",
                                return_value={"persisted": True},
                            ):
                                run_sector_live(
                                    "defence",
                                    max_workers=2,
                                    digest=True,
                                    macro_snapshot={"gift_nifty_change_pct": -0.8, "india_vix": 17.5},
                                )

    body = mock_email.call_args[0][0]
    assert "Macro risk filters: applied" in body
    assert "--- EOD Reconcile (Decision-first) ---" not in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_reconcile_report_only_suppresses_legacy_blocks(
    mock_load, mock_metrics, mock_email, monkeypatch
):
    from sector_audit import run_sector_live

    monkeypatch.setenv("TITAN_RECONCILE_MODE", "1")
    monkeypatch.setenv("TITAN_RECONCILE_REPORT_ONLY", "1")
    mock_load.return_value = [SectorInstrument("A", "NSE")]
    mock_metrics.side_effect = [
        {
            "ok": True,
            "symbol": "A",
            "exchange": "NSE",
            "audit": {
                "symbol": "A",
                "z_score": 1.0,
                "intent_score": 62.0,
                "effective_intent_score": 60.0,
                "absorption_ratio": 1.2,
                "return_1d_pct": 0.6,
                "rows": 30,
            },
            "error": None,
        }
    ]
    with patch("breeze_client.create_breeze_session", return_value=MagicMock()):
        with patch("brain.generate_sector_digest_narrative", return_value="One combined post"):
            with patch("supabase_log.save_audit_log"):
                with patch(
                    "analysis_store.persist_sector_run_analytics",
                    return_value={"persisted": True, "run_id": "test-report-only"},
                ):
                    with patch("analysis_store.update_sector_period_rollups"):
                        with patch(
                            "analysis_store.build_comparison_payload",
                            return_value={"enabled": False},
                        ):
                            with patch(
                                "analysis_store.persist_llm_digest_memory",
                                return_value={"persisted": True},
                            ):
                                run_sector_live("defence", max_workers=1, digest=True)

    body = mock_email.call_args[0][0]
    assert "--- EOD Reconcile (Decision-first) ---" in body
    assert "Report-only enforcement" in body
    assert "Per-symbol metrics" not in body
    assert "LLM forensic narrative" not in body


@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_fails_fast_on_expired_session(mock_load, mock_metrics):
    from sector_audit import run_sector_live

    mock_load.return_value = [SectorInstrument("A", "NSE")]
    with patch(
        "breeze_client.create_breeze_session",
        side_effect=RuntimeError("[Breeze] Session token expired."),
    ):
        with pytest.raises(RuntimeError, match="Session token expired"):
            run_sector_live("defence", max_workers=1, digest=True)
    mock_metrics.assert_not_called()


def test_prediction_reason_text_is_human_readable():
    from sector_audit import _prediction_reason_text

    audit = {
        "next_week_score": 62.0,
        "prediction_breakdown": {
            "day": {
                "tech_composite_term": 4.0,
                "ret1d_term": 3.12,
                "ema_term": 5.56,
                "ema_history_confidence": 1.0,
                "atr_penalty": 2.25,
            },
            "week": {
                "tech_composite_term": 5.5,
                "ret1d_term": 1.56,
                "ema_term": 9.27,
                "ema_history_confidence": 1.0,
                "atr_penalty": 0.90,
            },
            "penalties": [],
        },
    }
    text = _prediction_reason_text(audit)
    assert "confidence=medium" in text
    assert "drivers=" in text and "drags=" in text
    assert "penalties=none" in text
    assert "factors day[tech" in text and "week[tech" in text


def test_absorption_calibration_v2_fallback_default(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "high": closes, "low": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr("breeze_client.volume_participation_ratio", lambda _df: 9.0)
    monkeypatch.setattr("sector_audit._recent_absorption_samples", lambda *a, **k: [])
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )
    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), MagicMock(), inst, sector_id="defence")
    assert audit["absorption_ratio"] == 9.0
    assert audit["absorption_calibration"]["method"] == "fallback_default"
    assert audit["absorption_calibration"]["cap"] == pytest.approx(2.5)
    assert audit["absorption_calibrated_ratio"] == pytest.approx(2.5)
    assert audit["absorption_for_scoring"] <= 3.0


def test_absorption_calibration_v2_uses_historical_percentile(monkeypatch):
    from sector_audit import build_equity_live_audit

    closes = [100.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"close": closes, "high": closes, "low": closes, "volume": [1e6] * 30})
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr("breeze_client.volume_participation_ratio", lambda _df: 6.0)
    monkeypatch.setattr(
        "sector_audit._recent_absorption_samples",
        lambda *a, **k: [0.8, 1.0, 1.2, 1.4, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0],
    )
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "Post body",
    )
    inst = SectorInstrument("BHEL", "NSE")
    audit, _ = build_equity_live_audit(make_cfg(), MagicMock(), inst, sector_id="defence")
    assert audit["absorption_calibration"]["method"] == "symbol_daily_features_p90"
    assert audit["absorption_calibration"]["sample_count"] == 10
    assert audit["absorption_calibration"]["cap"] == pytest.approx(2.82, abs=1e-2)
    assert audit["absorption_calibrated_ratio"] == pytest.approx(2.82, abs=1e-2)


def test_predictive_scores_use_calibrated_absorption():
    from sector_audit import _predictive_scores

    base_audit = {
        "z_score": 0.0,
        "absorption_ratio": 9.0,
        "absorption_for_scoring": 1.2,
        "return_1d_pct": 0.0,
        "ema_200_distance_pct": 0.0,
        "atr_14_pct": 0.0,
        "effective_intent_score": 50.0,
    }
    day1, week1, _ = _predictive_scores(base_audit)
    base_audit["absorption_ratio"] = 1.2
    day2, week2, _ = _predictive_scores(base_audit)
    assert day1 == day2
    assert week1 == week2


def test_sell_signal_framework_states():
    from sector_audit import _derive_sell_signal

    hold_signal, hold_risk, _ = _derive_sell_signal(
        {
            "next_week_score": 72.0,
            "effective_intent_score": 64.0,
            "z_score": 1.8,
            "return_1d_pct": 1.1,
            "ema_200_distance_pct": 4.2,
            "atr_14_pct": 2.3,
            "fundamental_status": "strong",
        }
    )
    trim_signal, trim_risk, _ = _derive_sell_signal(
        {
            "next_week_score": 48.0,
            "effective_intent_score": 48.0,
            "z_score": -0.7,
            "return_1d_pct": -1.5,
            "return_5d_pct": -8.0,
            "return_21d_pct": -10.0,
            "return_63d_pct": -15.0,
            "return_126d_pct": -20.0,
            "ema_200_distance_pct": -1.2,
            "atr_14_pct": 3.2,
            "fundamental_status": "balanced",
            "event_risk_soon": True,
        }
    )
    exit_signal, exit_risk, reasons = _derive_sell_signal(
        {
            "next_week_score": 40.0,
            "effective_intent_score": 42.0,
            "z_score": -2.4,
            "return_1d_pct": -2.6,
            "ema_200_distance_pct": -7.0,
            "atr_14_pct": 6.8,
            "trap_exit_proxy": True,
            "macro_guardrail_applied": True,
            "fundamental_status": "weak",
        }
    )
    buy_signal, buy_risk, buy_reasons = _derive_sell_signal(
        {
            "next_week_score": 72.0,
            "effective_intent_score": 68.0,
            "z_score": 1.8,
            "return_1d_pct": 2.0,
            "return_5d_pct": 2.0,
            "return_10d_pct": 3.0,
            "rel_return_5d_vs_nifty_pct": 1.0,
            "cmf_20": 0.10,
            "obv_slope_20": 10.0,
            "ema_200_distance_pct": 4.2,
            "ema200_stretch_atr": 1.5,
            "atr_14_pct": 2.3,
            "adx_14": 30.0,
            "fundamental_status": "strong",
        }
    )
    assert buy_signal == "buy"
    assert buy_risk < 4.0
    assert buy_reasons
    assert hold_signal == "hold"
    assert hold_risk < 4.0
    assert trim_signal == "trim"
    assert 5.0 <= trim_risk < 7.0
    assert exit_signal == "exit-risk"
    assert exit_risk >= 7.0
    assert reasons


def test_apply_sector_cross_section_two_phase_orders_next_week_percentile():
    from sector_audit import _apply_sector_cross_section

    ok = [
        {
            "audit": {
                "next_week_score": 50.0,
                "next_day_score": 50.0,
                "effective_intent_score": 60.0,
                "intent_score": 60.0,
                "z_score": 0.0,
                "return_1d_pct": 0.0,
                "return_5d_pct": 0.0,
                "atr_14_pct": 2.0,
                "median_notional_inr_20d": 5e6,
            }
        },
        {
            "audit": {
                "next_week_score": 80.0,
                "next_day_score": 50.0,
                "effective_intent_score": 60.0,
                "intent_score": 60.0,
                "z_score": 0.0,
                "return_1d_pct": 0.0,
                "return_5d_pct": 0.0,
                "atr_14_pct": 2.0,
                "median_notional_inr_20d": 5e6,
            }
        },
    ]
    _apply_sector_cross_section(ok, score_percentiles=False)
    ok[0]["audit"]["next_week_score"] = 35.0
    ok[1]["audit"]["next_week_score"] = 75.0
    _apply_sector_cross_section(ok, score_percentiles=True)
    p0 = ok[0]["audit"]["sector_pctile_next_week_score"]
    p1 = ok[1]["audit"]["sector_pctile_next_week_score"]
    assert p0 < p1


def test_classify_error_code_timeout_variants():
    from sector_audit import _classify_error_code

    assert _classify_error_code("[Breeze] HAL (NSE) historical fetch timeout") == "data_fetch_timeout"
    assert (
        _classify_error_code("[Sector] no-progress watchdog timeout after 45.0s")
        == "sector_no_progress_watchdog"
    )


def test_enrich_audit_with_symbol_news_sets_fields(monkeypatch):
    from sector_audit import _enrich_audit_with_symbol_news

    cfg = make_cfg()
    inst = SectorInstrument("HAL", "NSE")
    audit = {"symbol": "HAL", "return_1d_pct": 0.5}
    recent = [
        {
            "title": "HAL order book expands",
            "sentiment_score": 0.4,
            "relevance_score": 0.7,
            "published_at": "2026-05-30T10:00:00+00:00",
        }
    ]
    monkeypatch.setattr("news_store.get_recent_news_for_symbol", lambda *a, **k: recent)
    monkeypatch.setattr(
        "news_audit.compute_news_sentiment_trend",
        lambda *a, **k: {"trend": "flat", "trend_score": 0.0, "item_count": 1},
    )
    monkeypatch.setattr(
        "news_audit.correlate_news_with_price_move",
        lambda *a, **k: {"aligned": True, "contradiction_strength": 0.0, "possible_reason": ""},
    )
    _enrich_audit_with_symbol_news(cfg, inst, audit)
    assert audit.get("news_count") == 1
    assert audit.get("news_sentiment_score") is not None
    assert "news_error" not in audit


def test_enrich_audit_with_symbol_news_sets_news_error_without_raising(monkeypatch):
    from sector_audit import _enrich_audit_with_symbol_news

    cfg = make_cfg()
    inst = SectorInstrument("HAL", "NSE")
    audit = {"symbol": "HAL"}

    def _fail(*_a, **_k):
        raise ConnectionError("news_feed unavailable")

    monkeypatch.setattr("news_store.get_recent_news_for_symbol", _fail)
    _enrich_audit_with_symbol_news(cfg, inst, audit)
    assert "news_error" in audit
    assert "news_feed unavailable" in str(audit["news_error"])


def test_format_symbol_metrics_dual_move_lines(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.5,
        "volume_participation_for_scoring": 1.0,
        "return_1d_pct": 5.74,
        "ohlc_bar_as_of_date": "2026-06-06",
        "session_move_vs_prev_close_pct": -2.1,
        "price_snapshot_ts": "06-Jun-2026 14:32:00",
        "atr_14_pct": 2.0,
        "next_week_score": 55.0,
        "sell_signal": "hold",
        "sell_signal_reasons": [],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line(
        {"symbol": "TAPE", "exchange": "NSE", "audit": audit}
    )
    assert "🟢⬆ 1D move (EOD): +5.74% · as of 2026-06-06" in text
    assert "🔴⬇ Session move (live): -2.10% · as of 14:32 IST" in text


def test_format_symbol_metrics_triple_z_score_lines_both_windows(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 1.8,
        "z_score_fast_20": 2.1,
        "z_score_slow": 1.4,
        "z_score_blend": "55%_20d_45%_60d",
        "volume_participation_ratio": 1.0,
        "return_1d_pct": 1.0,
        "ohlc_bar_as_of_date": "2026-06-06",
        "atr_14_pct": 2.0,
        "next_week_score": 55.0,
        "sell_signal": "hold",
        "sell_signal_reasons": [],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line(
        {"symbol": "TAPE", "exchange": "NSE", "audit": audit}
    )
    assert (
        "🟢⬆ 1D z-score (20D): +2.10 strong bull · as of 2026-06-06"
        in text
    )
    assert (
        "🟢⬆ 1D z-score (60D): +1.40 bullish · as of 2026-06-06"
        in text
    )
    assert (
        "   Z bands: ≥+2 strong bull · +1 to +2 bull · −1 to +1 mean · "
        "−2 to −1 bear · ≤−2 strong bear"
    ) in text
    assert (
        "🟢⬆ 1D z-score (blend, scoring): +1.80 bullish · as of 2026-06-06"
        in text
    )
    assert "1D z-score: " not in text


def test_format_symbol_metrics_triple_z_score_short_history_no_60d(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.9,
        "z_score_fast_20": 0.9,
        "z_score_slow": float("nan"),
        "z_score_blend": "20d_only",
        "volume_participation_ratio": 1.0,
        "return_1d_pct": 0.5,
        "ohlc_bar_as_of_date": "2026-06-06",
        "atr_14_pct": 2.0,
        "next_week_score": 55.0,
        "sell_signal": "hold",
        "sell_signal_reasons": [],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line(
        {"symbol": "TAPE", "exchange": "NSE", "audit": audit}
    )
    assert "1D z-score (20D):" in text
    assert "1D z-score (60D):" not in text
    assert "1D z-score (blend, scoring):" not in text
    assert "blend (scoring): +0.90" in text
    assert (
        "   Z bands: ≥+2 strong bull · +1 to +2 bull · −1 to +1 mean · "
        "−2 to −1 bear · ≤−2 strong bear"
    ) in text
    z_section = text.split("▸ 1D / Tape")[1].split("▸ Model outlook")[0]
    assert z_section.index("(20D):") < z_section.index("Z bands:")
    assert "blend (scoring)" in z_section


def test_format_symbol_metrics_dual_cmf_vpr_lines(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.5,
        "cmf_20": -0.254,
        "session_cmf_20": -0.03,
        "volume_participation_ratio": 1.42,
        "volume_participation_for_scoring": 2.1,
        "session_volume_participation_ratio": 0.85,
        "ohlc_bar_as_of_date": "2026-06-06",
        "price_snapshot_ts": "06-Jun-2026 14:32:00",
        "return_1d_pct": 1.0,
        "atr_14_pct": 2.0,
        "next_week_score": 55.0,
        "sell_signal": "hold",
        "sell_signal_reasons": [],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line(
        {"symbol": "TAPE", "exchange": "NSE", "audit": audit}
    )
    assert "🔴⬇ CMF (EOD): -0.254 distribution · as of 2026-06-06" in text
    assert "🟡➡ CMF (live): -0.030 neutral · as of 14:32 IST" in text
    assert "   CMF: >0.05 acc · −0.05 to 0.05 neutral · <−0.05 dist" in text
    assert "🟡➡ VPR (EOD): 1.42x above-avg · as of 2026-06-06" in text
    assert "🟡➡ VPR (live): 0.85x below-avg · as of 14:32 IST" in text
    assert "   VPR: ≥1.5 high · 1.0–1.49 above-avg · 0.7–0.99 below · <0.7 thin" in text
    assert "2.10x" not in text


def test_build_equity_live_audit_uses_prior_bar_when_session_incomplete(monkeypatch):
    from sector_audit import build_equity_live_audit

    df = pd.DataFrame(
        {
            "datetime": ["2026-06-04", "2026-06-05", "2026-06-06"],
            "close": [100.0, 105.0, 110.0],
            "volume": [1_000_000, 1_000_000, 500_000],
            "open": [100.0, 105.0, 108.0],
            "high": [101.0, 106.0, 111.0],
            "low": [99.0, 104.0, 107.0],
        }
    )
    monkeypatch.setattr("breeze_client.fetch_equity_data", lambda *a, **k: df)
    monkeypatch.setattr("market_calendar.is_cash_market_session_open_ist", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "breeze_client.fetch_equity_quote",
        lambda *a, **k: {
            "ltp": 107.8,
            "previous_close": 110.0,
            "ltp_percent_change": -2.0,
            "ltt": "06-Jun-2026 14:32:00",
            "open": 108.0,
            "high": 111.0,
            "low": 107.0,
        },
    )
    monkeypatch.setattr(
        "brain.generate_titan_narrative",
        lambda audit, api_key=None, api_keys=None: "",
    )
    metrics_df = df.iloc[:-1].reset_index(drop=True)
    monkeypatch.setattr(
        "sector_audit._prepare_ohlc_for_metrics",
        lambda raw_df, now_ist=None: (
            metrics_df,
            {
                "ohlc_bar_as_of_date": "2026-06-05",
                "ohlc_bar_incomplete": True,
                "session_open": True,
                "sorted_df": raw_df,
                "metrics_df": metrics_df,
            },
        ),
    )

    inst = SectorInstrument("HAL", "NSE")
    audit, _ = build_equity_live_audit(
        make_cfg(),
        MagicMock(),
        inst,
        sector_id="defence",
        with_narrative=False,
    )
    assert audit["ohlc_bar_as_of_date"] == "2026-06-05"
    assert audit["ohlc_bar_incomplete"] is True
    assert abs(audit["return_1d_pct"] - ((105.0 / 100.0) - 1.0) * 100.0) < 0.01
    assert abs(audit["session_move_vs_prev_close_pct"] + 2.0) < 0.5
    assert audit["price_snapshot_ts"] == "06-Jun-2026 14:32:00"
    assert not math.isnan(audit["session_cmf_20"])
    assert not math.isnan(audit["session_volume_participation_ratio"])
    assert audit["session_volume_participation_ratio"] == pytest.approx(0.5, rel=0.01)


@pytest.mark.parametrize(
    "to_high,above_low,expected",
    [
        (float("nan"), float("nan"), "n/a"),
        (-0.5, 9.0, "near-high"),
        (-2.0, 0.8, "near-low"),
        (-4.0, 12.0, "mid-range"),
    ],
)
def test_range_position_context_honest_labels(to_high, above_low, expected):
    from sector_audit import _range_position_context

    assert _range_position_context(to_high, above_low) == expected


def test_compact_digest_section_headers_preserved(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _format_symbol_metrics_line

    audit = {
        "effective_intent_score": 50.0,
        "z_score": 0.5,
        "volume_participation_ratio": 1.0,
        "return_1d_pct": 1.0,
        "atr_14_pct": 2.0,
        "next_week_score": 55.0,
        "sell_signal": "hold",
        "sell_signal_reasons": [],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
    }
    text = _format_symbol_metrics_line({"symbol": "HDR", "exchange": "NSE", "audit": audit})
    for header in ("▸ Trend Regime (14D)", "▸ 20D Money Flow", "▸ 1D / Tape", "▸ Model outlook"):
        assert header in text


def test_digest_shadow_gate_notes_in_context(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _digest_shadow_gate_notes, _format_symbol_metrics_line

    audit = {
        "sector_key": "banks_psu",
        "effective_intent_score": 58.0,
        "z_score": 1.2,
        "volume_participation_ratio": 1.4,
        "return_1d_pct": 0.8,
        "atr_14_pct": 2.1,
        "next_week_score": 59.5,
        "sell_signal": "hold",
        "sell_signal_reasons": ["monitor trend"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "shadow_gates": [
            {
                "gate": "sector_regime",
                "mode": "shadow",
                "triggered": True,
                "would": "withhold/damp new buys",
                "reasons": ["breadth 33% < floor 40%"],
                "breadth_now": 33.0,
                "score_multiplier": 1.0,
                "withhold": False,
            },
            {
                "gate": "v2_risk_label",
                "mode": "shadow",
                "triggered": False,
                "would": "allow",
                "v2_label": "hold",
                "score_multiplier": 1.0,
                "withhold": False,
            },
        ],
        "signal_overext_ceiling": {
            "mode": "shadow",
            "would_ceiling": "accumulate",
            "applied_ceiling": None,
            "hot": ["stretch 3.8", "z 2.4"],
        },
    }
    notes = _digest_shadow_gate_notes(audit)
    assert any("regime gate would damp" in n for n in notes)
    assert any("overext ceiling gate would cap" in n for n in notes)

    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "▸ Context" in text
    assert "• Shadow (not enforced): regime gate would damp — breadth 33% < floor 40%" in text
    assert "• Shadow (not enforced): overext ceiling gate would cap — stretch 3.8, z 2.4" in text
    assert "hold" in text.splitlines()[0].lower()


def test_digest_applied_gate_notes_in_context(monkeypatch):
    monkeypatch.delenv("TITAN_DIGEST_VERBOSE_SYMBOLS", raising=False)
    from sector_audit import _digest_shadow_gate_notes, _format_symbol_metrics_line

    audit = {
        "sector_key": "telecom",
        "effective_intent_score": 52.0,
        "z_score": 2.1,
        "volume_participation_ratio": 1.2,
        "return_1d_pct": -0.4,
        "atr_14_pct": 2.4,
        "next_week_score": 51.0,
        "sell_signal": "hold",
        "sell_signal_reasons": ["overextended"],
        "prediction_breakdown": {"week": {}, "day": {}, "penalties": []},
        "shadow_gates": [
            {
                "gate": "delivery_churn",
                "mode": "damp",
                "triggered": True,
                "would": "damp/withhold (churn)",
                "reasons": ["avg delivery 13% < floor 20%"],
                "delivery_avg": 13.0,
                "delivery_latest": 11.0,
                "score_multiplier": 0.5,
                "withhold": False,
            },
            {
                "gate": "institutional",
                "mode": "damp",
                "triggered": True,
                "would": "damp (institutional backdrop)",
                "reasons": ["FII net -1082 Cr"],
                "score_multiplier": 0.85,
                "withhold": False,
            },
        ],
        "signal_overext_ceiling": {
            "mode": "enforce",
            "would_ceiling": "hold",
            "applied_ceiling": "hold",
            "hot": ["stretch 4.57"],
        },
    }
    notes = _digest_shadow_gate_notes(audit)
    assert any("Applied: delivery/churn gate" in n for n in notes)
    assert any("Applied: institutional gate" in n for n in notes)
    assert any("Applied: overext ceiling gate" in n for n in notes)

    text = _format_symbol_metrics_line({"symbol": "NETWEB", "exchange": "NSE", "audit": audit})
    assert "• Applied: delivery/churn gate — rank damped (×0.50)" in text
    assert "• Applied: institutional gate — rank damped (×0.85)" in text
    assert "• Applied: overext ceiling gate — label capped to hold" in text


def test_bridge_priority_shadow_context_rehydrates_persisted_modes(monkeypatch):
    monkeypatch.setenv("TITAN_DELIVERY_GATE_MODE", "damp")
    monkeypatch.setenv("TITAN_INSTITUTIONAL_GATE_MODE", "damp")
    from sector_audit import _bridge_priority_shadow_context, _digest_shadow_gate_notes

    class _Cfg:
        supabase_url = "http://example"
        supabase_key = "key"

    meta = {
        ("NETWEB", "NSE"): {
            "shadow_gates": [
                {
                    "gate": "delivery_churn",
                    "mode": "shadow",
                    "triggered": True,
                    "would": "damp/withhold (churn)",
                    "reasons": ["avg delivery 13% < floor 35%"],
                    "delivery_avg": 13.0,
                    "score_multiplier": 1.0,
                    "withhold": False,
                },
                {
                    "gate": "institutional",
                    "mode": "shadow",
                    "triggered": True,
                    "would": "damp (institutional backdrop)",
                    "reasons": ["FII net -1082 Cr", "DII net +5341 Cr"],
                    "score_multiplier": 1.0,
                    "withhold": False,
                },
            ],
            "institutional_context": {
                "risk_off": True,
                "mode": "shadow",
                "fii_net_crs": -1082.0,
                "dii_net_crs": 5341.0,
                "gate_applied": False,
            },
        }
    }

    monkeypatch.setattr(
        "sector_audit._load_priority_ranking_meta",
        lambda cfg, sector_key, symbol_keys: meta,
    )

    ok_results = [{"symbol": "NETWEB", "exchange": "NSE", "audit": {}}]
    _bridge_priority_shadow_context(_Cfg(), "ai", ok_results)
    notes = _digest_shadow_gate_notes(ok_results[0]["audit"])
    assert any("Applied: delivery/churn gate" in n for n in notes)
    assert any("Applied: institutional gate" in n for n in notes)
    assert not any("Shadow (not enforced): delivery/churn gate" in n for n in notes)
    assert not any("Shadow (not enforced): institutional gate" in n for n in notes)
