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
    assert "🟢⬆" in text or "🟡➡" in text or "🔴⬇" in text
    assert "trend regime (14d)" in text.lower()
    assert "source: +di" in text.lower()
    assert "20d range position" in text.lower()
    assert "volatility vs 3m baseline" in text.lower()
    assert "money flow trend (20d)" in text.lower()
    assert "distance above long-term trend (ema200)" in text.lower()
    assert "typical daily swing (atr14)" in text.lower()
    assert "tape snapshot" in text.lower()
    assert "Why this action" in text
    assert "\n" in text
    assert "model read confidence" in text.lower()


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
    assert "🟡➡ Trend regime (14D): Sideways (ADX n/a; n/a; source: direction source unavailable)" in text
    assert "🟡➡ 20D Range Position: n/a% to 20D high · n/a% above 20D low (range context unavailable)" in text
    assert "🟡➡ Volatility vs 3M baseline: n/ax (n/a; bands: <0.90 low, 0.90-1.10 normal, >1.10 high)" in text
    assert (
        "🟡➡ Money flow trend (20D): n/a "
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
        },
    }
    text = _format_symbol_metrics_line({"symbol": "HAL", "exchange": "NSE", "audit": audit})
    assert "Global news relation:" in text
    assert "affected_metric=momentum 5D" in text
    assert "direction=tailwind" in text


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
    assert "effective_intent_score" in audit
    assert audit.get("z_score_blend") == "20d_only"
    assert "high_volume_down_day_proxy" in audit


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
