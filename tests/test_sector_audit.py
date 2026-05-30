"""Sector equity audit (cash metrics, mocked Breeze/Gemini)."""

import math
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
    assert "TRIM" in text or "trim" in text.lower()
    assert "1w outlook" in text.lower()
    assert "neutral band" in text.lower()
    assert "trend regime (14d)" in text.lower()
    assert "strength bands: <20 sideways, 20-24 weak trend, >=25 strong trend" in text.lower()
    assert "direction rule: +di" in text.lower()
    assert "20d range position" in text.lower()
    assert "thresholds: near-high >=-1%, near-low <=1%" in text.lower()
    assert "volatility vs 3m baseline" in text.lower()
    assert "money flow trend (20d)" in text.lower()
    assert "bands: >0.05 accumulation, -0.05 to 0.05 neutral, < -0.05 distribution" in text
    assert "sector-relative rank" in text.lower()
    assert "bands: leader >=67, average 34-66, laggard <=33" in text
    assert "very short horizon (1d outlook):" in text.lower()
    assert "distance above long-term trend (ema200)" in text.lower()
    assert "typical daily swing (atr14)" in text.lower()
    assert "bands: <2.0 calm, 2.0-4.0 moderate, >4.0 elevated" in text
    assert "tape snapshot" in text.lower()
    assert "bands: >=+1 strong up, -1 to +1 muted, <=-1 weak" in text
    assert "bands: >=1.5 high, 1.0-1.49 above-avg, 0.7-0.99 below-avg, <0.7 thin" in text
    assert "bands: >=70 strong, 55-69 constructive, 45-54 neutral, 35-44 caution, <35 defensive" in text
    assert "bands: >=70 high-long, 55-69 moderate-long, 45-54 neutral, 30-44 defensive, <30 high-defensive" in text
    assert "Why this action" in text
    assert "\n" in text
    assert "model read confidence" in text.lower()
    assert "bands: >=70 high, 55-69 medium, <55 low" in text
    assert "🟡➡ 1W outlook:" in text
    assert "🟡➡ Technical intent:" in text
    assert "🟢⬆ Trend regime (14D): Buy trend" in text
    assert "🔴⬇ 1D move: -4.28%" in text
    assert any(f"{icon} Tape snapshot" in text for icon in ("🟢⬆", "🟡➡", "🔴⬇"))
    assert any(f"{icon} Sector-relative rank" in text for icon in ("🟢⬆", "🟡➡", "🔴⬇"))
    assert any(
        f"{icon} Technical intent percentile: 62.00" in text for icon in ("🟢⬆", "🟡➡", "🔴⬇")
    )


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
    assert (
        "Trend regime (14D): Sideways (ADX n/a; strength n/a; strength bands: <20 sideways, 20-24 weak trend, >=25 strong trend; direction rule: direction source unavailable)"
        in text
    )
    assert "20D Range Position: n/a% to 20D high \u00b7 n/a% above 20D low (near-high (within ~1% of 20D high); thresholds: near-high >=-1%, near-low <=1%)" in text
    assert "Volatility vs 3M baseline: n/ax (n/a; bands: <0.90 low, 0.90-1.10 normal, >1.10 high)" in text
    assert (
        "Money flow trend (20D): n/a "
        "(n/a; bands: >0.05 accumulation, -0.05 to 0.05 neutral, < -0.05 distribution)" in text
    )


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
    assert "Global news relation:" in text
    assert "affected_metric=momentum 5D" in text
    assert "direction=tailwind" in text
    assert "bands: >=0.75 high, 0.50-0.74 medium, <0.50 low" in text
    assert "News evidence: net_news_impact_score=0.2142" in text
    assert "source=Reuters" in text
    assert "published_at=2026-05-30T08:00:00+00:00" in text
    assert "impact_contribution_score=0.1822" in text


def test_apply_global_news_correlation_uses_explicit_fallback_when_sector_missing(monkeypatch):
    from sector_audit import _apply_global_news_correlation
    from sector_registry import SectorInstrument

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
    ok_results = [{"audit": {"prediction_breakdown": {"week": {"ema_term": 1.3}}}}]
    meta = _apply_global_news_correlation(make_cfg(), sector_id="ai", ok_results=ok_results)
    corr = ok_results[0]["audit"]["news_correlation"]
    assert meta["applied"] is True
    assert "fallback_label" in corr
    assert "sector_specific_match_missing_using_global_market_driver" == corr["fallback_label"]
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
            "fallback_label": "sector_specific_match_missing_using_global_market_driver",
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "Global news relation:" in text
    assert "fallback=sector_specific_match_missing_using_global_market_driver" in text


def test_apply_global_news_correlation_sets_line_for_all_audits_when_snapshot_empty(monkeypatch):
    from sector_audit import _apply_global_news_correlation, _news_correlation_line

    snapshot = {"source": "unavailable", "news_items": [], "sector_scores": {}}
    monkeypatch.setattr("sector_priority.resolve_global_news_snapshot", lambda _cfg: snapshot)
    monkeypatch.setattr("sector_priority.load_priority_instruments", lambda _cfg, sector_key, top_n=None: [])
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
        assert "Global news relation:" in line
        assert "fallback=sector_specific_match_missing_no_market_driver" in line


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
    monkeypatch.setattr("sector_priority.load_priority_instruments", lambda _cfg, sector_key, top_n=None: [])

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
    assert "One combined post" in body
    assert "Per-symbol metrics" in body
    assert "Risk overlays" in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_macro_guardrail_applied(mock_load, mock_metrics, mock_email):
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
    assert "Macro guardrail applied: yes" in body


@patch("email_notify.send_success_post_email")
@patch("sector_audit._process_one_metrics")
@patch("sector_audit.load_sector_instruments")
def test_run_sector_live_reconcile_report_only_suppresses_legacy_blocks(
    mock_load, mock_metrics, mock_email, monkeypatch
):
    from sector_audit import run_sector_live

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
            "next_week_score": 53.0,
            "effective_intent_score": 50.0,
            "z_score": -0.7,
            "return_1d_pct": -0.4,
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
            "ema_200_distance_pct": 4.2,
            "atr_14_pct": 2.3,
            "fundamental_status": "strong",
        }
    )
    assert buy_signal == "buy"
    assert buy_risk < 4.0
    assert buy_reasons
    assert hold_signal == "hold"
    assert hold_risk < 4.0
    assert trim_signal == "trim"
    assert 4.0 <= trim_risk < 7.0
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
