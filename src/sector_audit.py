"""Sector-wide equity audits: CSV universe, parallel Breeze fetches, Supabase + digest email."""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config_loader import TitanConfig, load_config
from sector_registry import SectorInstrument, load_sector_instruments

logger = logging.getLogger(__name__)

# Parallel sector fetches (each worker opens its own Breeze session; keep modest for API limits).
MAX_WORKERS = 4

# Serialize Gemini calls so sector threads do not burst past rate limits together.
_GEMINI_SECTOR_LOCK = threading.Lock()
IST = ZoneInfo("Asia/Kolkata")


def _fmt_metric(x: Any, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(v):
        return "n/a"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return f"{v:.{digits}f}"


def _z_label(z: Any) -> str:
    try:
        v = float(z)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 2.0:
        return "strong bullish deviation"
    if v >= 1.0:
        return "bullish deviation"
    if v <= -2.0:
        return "strong bearish deviation"
    if v <= -1.0:
        return "bearish deviation"
    return "near mean"


def _absorption_label(absorption: Any) -> str:
    try:
        v = float(absorption)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if math.isinf(v) and v > 0:
        return "extreme relative volume"
    if v >= 1.5:
        return "high participation"
    if v >= 1.0:
        return "above average participation"
    if v >= 0.7:
        return "below average participation"
    return "thin participation"


def _intent_label(intent: Any) -> str:
    try:
        v = float(intent)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(v):
        return "unknown"
    if v >= 70:
        return "high conviction long bias"
    if v >= 55:
        return "moderate long bias"
    if v >= 45:
        return "balanced / neutral"
    if v >= 30:
        return "moderate defensive bias"
    return "high defensive bias"


def _format_symbol_metrics_line(result: dict[str, Any]) -> str:
    symbol = result["symbol"]
    exchange = result["exchange"]
    audit = result["audit"]
    z = audit.get("z_score")
    intent = audit.get("effective_intent_score", audit.get("intent_score"))
    absorption = audit.get("absorption_ratio")
    ret1d = audit.get("return_1d_pct")
    ema_dist = audit.get("ema_200_distance_pct")
    atr_pct = audit.get("atr_14_pct")
    rows = audit.get("rows")
    flags: list[str] = []
    if audit.get("panic_absorption_proxy"):
        flags.append("panic-absorption")
    if audit.get("trap_exit_proxy"):
        flags.append("up-move-trap")
    if audit.get("cluster_guardrail_applied"):
        flags.append("cluster-downgraded")
    if audit.get("macro_guardrail_applied"):
        flags.append("macro-risk-throttle")
    if audit.get("event_risk_soon"):
        flags.append("event-risk<=3d")
    flag_text = ", ".join(flags) if flags else "none"
    return (
        f"{symbol} ({exchange}) | intent {_fmt_metric(intent)} [{_intent_label(intent)}] "
        f"| z {_fmt_metric(z)} [{_z_label(z)}] | absorption {_fmt_metric(absorption, 3)} "
        f"[{_absorption_label(absorption)}] | ret1d {_fmt_metric(ret1d)}% "
        f"| ema200_delta {_fmt_metric(ema_dist)}% | atr14 {_fmt_metric(atr_pct)}% "
        f"| flags={flag_text} | rows {rows}"
    )


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _bucket_name(audit: dict[str, Any]) -> str:
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    z = _safe_float(audit.get("z_score"))
    ab = _safe_float(audit.get("absorption_ratio"))
    if audit.get("trap_exit_proxy"):
        return "trap-risk"
    if (not math.isnan(eff) and eff >= 65.0) and (not math.isnan(z) and z >= 2.0) and (
        not math.isnan(ab) and ab >= 1.0
    ):
        return "high-conviction-momentum"
    if (not math.isnan(eff) and eff >= 55.0) or (not math.isnan(z) and z >= 1.5):
        return "constructive-watchlist"
    return "neutral-weak"


def _short_reason(audit: dict[str, Any]) -> str:
    z = _safe_float(audit.get("z_score"))
    ab = _safe_float(audit.get("absorption_ratio"))
    bits: list[str] = []
    if not math.isnan(z):
        bits.append(f"z={z:.2f}")
    if not math.isnan(ab):
        bits.append(f"abs={ab:.2f}")
    if audit.get("trap_exit_proxy"):
        bits.append("trap-flag")
    if audit.get("macro_guardrail_applied"):
        bits.append("macro-throttle")
    return ", ".join(bits[:3]) if bits else "insufficient data"


def _apply_cluster_guardrails(ok_results: list[dict[str, Any]]) -> tuple[float, int]:
    if not ok_results:
        return 0.0, 0
    red_count = 0
    for r in ok_results:
        ret = _safe_float(r["audit"].get("return_1d_pct"))
        if not math.isnan(ret) and ret <= -1.0:
            red_count += 1
    red_ratio = red_count / len(ok_results)
    applied = 0
    if red_ratio > 0.70:
        for r in ok_results:
            a = r["audit"]
            intent = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
            if not math.isnan(intent) and intent >= 55.0:
                a["effective_intent_score"] = min(intent, 50.0)
                a["cluster_guardrail_applied"] = True
                a["cluster_guardrail_reason"] = (
                    f"cluster breadth risk: {red_count}/{len(ok_results)} names <= -1% day return"
                )
                applied += 1
    return red_ratio, applied


def _apply_macro_guardrails(
    ok_results: list[dict[str, Any]], macro_snapshot: dict[str, Any] | None
) -> tuple[bool, str]:
    if not macro_snapshot:
        return False, "macro snapshot not provided"
    gift = _safe_float(macro_snapshot.get("gift_nifty_change_pct"))
    vix = _safe_float(macro_snapshot.get("india_vix"))
    risk_on = (not math.isnan(gift) and gift < -0.5) or (not math.isnan(vix) and vix > 18.0)
    if not risk_on:
        return False, "macro trigger not active"
    for r in ok_results:
        a = r["audit"]
        base = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
        if math.isnan(base):
            continue
        a["effective_intent_score"] = round(base * 0.5, 2)
        a["macro_guardrail_applied"] = True
        a["macro_guardrail_reason"] = (
            f"GIFT={_fmt_metric(gift)}%, IndiaVIX={_fmt_metric(vix)} (trigger: GIFT<-0.5 or VIX>18)"
        )
    return True, f"GIFT={_fmt_metric(gift)}%, IndiaVIX={_fmt_metric(vix)}"


def _apply_event_guardrails(ok_results: list[dict[str, Any]]) -> int:
    adjusted = 0
    for r in ok_results:
        a = r["audit"]
        if not a.get("event_risk_soon"):
            continue
        base = _safe_float(a.get("effective_intent_score", a.get("intent_score")))
        if math.isnan(base):
            continue
        a["effective_intent_score"] = round(base * 0.85, 2)
        a["event_guardrail_applied"] = True
        adjusted += 1
    return adjusted


def build_equity_live_audit(
    cfg: TitanConfig,
    breeze: Any,
    inst: SectorInstrument,
    *,
    sector_id: str,
    lookback_calendar_days: int = 60,
    with_narrative: bool = True,
    strict_data: bool = False,
    event_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Cash-market metrics only (z-score, volume absorption, intent blend).
    Option chain / PCR are skipped for sector equities (expiry rules differ per name); flagged in audit.

    With default ``strict_data=False``, empty Breeze history returns ``skipped_no_data`` instead of
    raising (sector runs skip that symbol). Pass ``strict_data=True`` to fail hard on no rows.
    """
    import pandas as pd

    from breeze_client import fetch_equity_data, volume_absorption_ratio
    from titan_engine import calculate_atr, calculate_ema, calculate_intent_score, calculate_z_score

    df = fetch_equity_data(
        cfg,
        inst.symbol,
        inst.exchange,
        breeze=breeze,
        lookback_calendar_days=lookback_calendar_days,
    )
    if df.empty:
        if strict_data:
            raise RuntimeError(
                f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); task BLOCKED"
            )
        skip: dict[str, Any] = {
            "benchmark": "equity",
            "sector_mode": True,
            "sector": sector_id,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "skipped_no_data": True,
            "z_score": float("nan"),
            "absorption_ratio": float("nan"),
            "pcr": float("nan"),
            "put_oi": 0.0,
            "call_oi": 0.0,
            "oi_wall": {"strike": float("nan"), "oi": float("nan")},
            "option_expiry": None,
            "intent_score": float("nan"),
            "rows": 0,
            "option_chain_unavailable": True,
        }
        return skip, ""
    close_col = "close" if "close" in df.columns else df.columns[-1]
    series = pd.to_numeric(df[close_col], errors="coerce")
    series_non_na = series.dropna()
    close_last = float(series_non_na.iloc[-1]) if not series_non_na.empty else float("nan")
    close_prev = float(series_non_na.iloc[-2]) if len(series_non_na) >= 2 else float("nan")
    ret1d = (
        ((close_last / close_prev) - 1.0) * 100.0
        if (not math.isnan(close_last) and not math.isnan(close_prev) and close_prev != 0.0)
        else float("nan")
    )
    z = calculate_z_score(series, window=20)
    absorption = volume_absorption_ratio(df)
    ema_200 = calculate_ema(series, span=200)
    ema_distance_pct = (
        ((close_last / ema_200) - 1.0) * 100.0
        if (not math.isnan(close_last) and not math.isnan(ema_200) and ema_200 != 0.0)
        else float("nan")
    )
    atr_14 = calculate_atr(df, window=14)
    atr_14_pct = (
        (atr_14 / close_last) * 100.0
        if (not math.isnan(atr_14) and not math.isnan(close_last) and close_last != 0.0)
        else float("nan")
    )
    atr_break_multiple = (
        abs(close_last - ema_200) / atr_14
        if (
            not math.isnan(close_last)
            and not math.isnan(ema_200)
            and not math.isnan(atr_14)
            and atr_14 > 0.0
        )
        else float("nan")
    )
    pcr = float("nan")
    intent = calculate_intent_score(pcr, z, absorption)
    panic_absorption_proxy = (
        not math.isnan(ret1d) and ret1d < 0.0 and not math.isnan(absorption) and absorption >= 1.5
    )
    trap_exit_proxy = (
        not math.isnan(ret1d) and ret1d > 0.0 and not math.isnan(absorption) and absorption <= 0.5
    )
    event_info = _event_flags_for_symbol(inst.symbol, event_snapshot)
    audit: dict[str, Any] = {
        "benchmark": "equity",
        "sector_mode": True,
        "sector": sector_id,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "z_score": z,
        "absorption_ratio": absorption,
        "close_last": close_last,
        "return_1d_pct": ret1d,
        "ema_200": ema_200,
        "ema_200_distance_pct": ema_distance_pct,
        "atr_14": atr_14,
        "atr_14_pct": atr_14_pct,
        "atr_break_multiple": atr_break_multiple,
        "structural_break_proxy": (
            not math.isnan(atr_break_multiple) and atr_break_multiple >= 1.5
        ),
        "panic_absorption_proxy": panic_absorption_proxy,
        "trap_exit_proxy": trap_exit_proxy,
        **event_info,
        "history_lt_200_sessions": len(series_non_na) < 200,
        "pcr": pcr,
        "put_oi": 0.0,
        "call_oi": 0.0,
        "oi_wall": {"strike": float("nan"), "oi": float("nan")},
        "option_expiry": None,
        "intent_score": intent,
        "effective_intent_score": intent,
        "rows": len(df),
        "option_chain_unavailable": True,
    }
    if not with_narrative:
        return audit, ""
    from brain import generate_titan_narrative

    with _GEMINI_SECTOR_LOCK:
        post = generate_titan_narrative(audit, api_keys=cfg.gemini_api_keys)
    return audit, post


def _event_flags_for_symbol(
    symbol: str, event_snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(event_snapshot, dict):
        return {
            "event_risk_present": False,
            "event_risk_soon": False,
            "event_days_to_next": None,
            "event_types": [],
        }
    events = event_snapshot.get("events")
    if not isinstance(events, list):
        events = []
    sym = "".join(ch for ch in symbol.upper() if ch.isalnum())
    today = datetime.now(IST).date()
    days: list[int] = []
    types: list[str] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        es = "".join(ch for ch in str(raw.get("symbol", "")).upper() if ch.isalnum())
        if es != sym:
            continue
        t = str(raw.get("type", "")).strip().lower()
        if t:
            types.append(t)
        ds = str(raw.get("date", "")).strip()
        try:
            d = datetime.fromisoformat(ds).date()
        except ValueError:
            continue
        days.append((d - today).days)
    if not days:
        return {
            "event_risk_present": False,
            "event_risk_soon": False,
            "event_days_to_next": None,
            "event_types": sorted(set(types)),
        }
    nxt = min(days)
    return {
        "event_risk_present": True,
        "event_risk_soon": nxt <= 3,
        "event_days_to_next": nxt,
        "event_types": sorted(set(types)),
    }


def _process_one(
    cfg: TitanConfig,
    sector_id: str,
    inst: SectorInstrument,
    *,
    event_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from breeze_client import create_breeze_session
    from supabase_log import save_audit_log

    breeze = create_breeze_session(cfg)
    audit, post = build_equity_live_audit(
        cfg,
        breeze,
        inst,
        sector_id=sector_id,
        strict_data=False,
        event_snapshot=event_snapshot,
    )
    if audit.get("skipped_no_data"):
        logger.warning(
            "Sector instrument skipped (no Breeze data): %s %s",
            inst.symbol,
            inst.exchange,
        )
        return {
            "ok": False,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "post": "",
            "error": f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); skipped",
        }
    save_audit_log({"audit": audit, "post": post}, cfg)
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "audit": audit,
        "post": post,
        "error": None,
    }


def _process_one_metrics(
    cfg: TitanConfig,
    sector_id: str,
    inst: SectorInstrument,
    *,
    event_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Breeze + metrics only; no Gemini (used with --sector-digest)."""
    from breeze_client import create_breeze_session

    breeze = create_breeze_session(cfg)
    audit, _ = build_equity_live_audit(
        cfg,
        breeze,
        inst,
        sector_id=sector_id,
        with_narrative=False,
        strict_data=False,
        event_snapshot=event_snapshot,
    )
    if audit.get("skipped_no_data"):
        logger.warning(
            "Sector instrument skipped (no Breeze data): %s %s",
            inst.symbol,
            inst.exchange,
        )
        return {
            "ok": False,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "audit": None,
            "error": f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); skipped",
        }
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "audit": audit,
        "error": None,
    }


def run_sector_live(
    sector_id: str,
    *,
    max_workers: int | None = None,
    max_symbols: int | None = None,
    digest: bool = True,
    macro_snapshot: dict[str, Any] | None = None,
    event_snapshot: dict[str, Any] | None = None,
    instruments_override: list[SectorInstrument] | None = None,
) -> None:
    from email_notify import send_success_post_email
    from breeze_client import create_breeze_session

    cfg = load_config()
    # Preflight Breeze auth once to fail fast on expired tokens.
    # Without this, each worker thread would emit the same auth stacktrace.
    create_breeze_session(cfg)
    instruments = instruments_override if instruments_override is not None else load_sector_instruments(sector_id)
    if not instruments:
        raise RuntimeError(f"[Sector] No instruments loaded for sector {sector_id!r}")

    if max_symbols is not None:
        instruments = instruments[: max(0, int(max_symbols))]

    workers = max_workers if max_workers is not None else MAX_WORKERS
    workers = max(1, min(int(workers), 16))

    results: list[dict[str, Any]] = []
    worker = _process_one_metrics if digest else _process_one
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(worker, cfg, sector_id, inst, event_snapshot=event_snapshot): inst
            for inst in instruments
        }
        for fut in as_completed(future_map):
            inst = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Sector instrument failed: %s %s", inst.symbol, inst.exchange)
                err_row: dict[str, Any] = {
                    "ok": False,
                    "symbol": inst.symbol,
                    "exchange": inst.exchange,
                    "error": str(e),
                }
                if digest:
                    err_row["audit"] = None
                else:
                    err_row["post"] = ""
                results.append(err_row)

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        raise RuntimeError(
            f"[Sector] All {len(results)} instruments failed for sector {sector_id!r}"
        )

    if digest:
        from analysis_store import persist_sector_run_analytics
        from analysis_store import (
            build_comparison_payload,
            persist_llm_digest_memory,
            quality_checks_for_run,
            update_sector_period_rollups,
        )
        from brain import generate_sector_digest_narrative
        from supabase_log import save_audit_log

        ok_results = [r for r in results if r.get("ok")]
        audits = [r["audit"] for r in ok_results]
        red_ratio, cluster_downgrades = _apply_cluster_guardrails(ok_results)
        event_adjustments = _apply_event_guardrails(ok_results)
        macro_applied, macro_reason = _apply_macro_guardrails(ok_results, macro_snapshot)
        persist_meta = persist_sector_run_analytics(
            cfg,
            sector=sector_id,
            audits=audits,
            mode="sector_digest",
            ok_count=ok_count,
            total_count=len(results),
        )
        update_sector_period_rollups(cfg, sector=sector_id)
        comparison = build_comparison_payload(cfg, sector=sector_id)
        qc_warnings = quality_checks_for_run(audits, comparison=comparison)
        with _GEMINI_SECTOR_LOCK:
            post = generate_sector_digest_narrative(
                audits,
                sector_id=sector_id,
                comparison_context=comparison if comparison.get("enabled") else None,
                api_keys=cfg.gemini_api_keys,
            )
        for r in ok_results:
            save_audit_log({"audit": r["audit"], "post": post}, cfg)

        if persist_meta.get("persisted") and persist_meta.get("run_id"):
            persist_llm_digest_memory(
                cfg,
                run_id=str(persist_meta["run_id"]),
                sector=sector_id,
                prompt_facts=comparison if comparison.get("enabled") else {"enabled": False},
                output_text=post,
                model_name=None,
            )

        by_bucket: dict[str, list[dict[str, Any]]] = {
            "high-conviction-momentum": [],
            "constructive-watchlist": [],
            "neutral-weak": [],
            "trap-risk": [],
        }
        for r in ok_results:
            by_bucket[_bucket_name(r["audit"])].append(r)

        today = comparison.get("today") if isinstance(comparison, dict) else {}
        dlt = comparison.get("delta") if isinstance(comparison, dict) else {}
        leaders = comparison.get("leaders", []) if isinstance(comparison, dict) else []
        laggards = comparison.get("laggards", []) if isinstance(comparison, dict) else []

        lines = [
            f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded "
            f"(digest mode: 1 Gemini call)\n",
            "",
            "--- Executive snapshot ---",
            f"Regime: {(comparison.get('regime') if isinstance(comparison, dict) else 'n/a')}",
            f"Avg effective intent: {_fmt_metric(today.get('avg_effective_intent_score') if isinstance(today, dict) else None)} "
            f"(vs 7d {_fmt_metric(dlt.get('avg_effective_intent_vs_7d') if isinstance(dlt, dict) else None)}, "
            f"vs 30d {_fmt_metric(dlt.get('avg_effective_intent_vs_30d') if isinstance(dlt, dict) else None)})",
            f"Breadth above EMA200: {_fmt_metric(today.get('breadth_above_ema200_pct') if isinstance(today, dict) else None)}%",
            f"Participation breadth (absorption>1): {_fmt_metric(today.get('pct_absorption_gt_1') if isinstance(today, dict) else None)}%",
            "",
            "--- Movement summary ---",
        ]
        if leaders:
            lines.append(
                "Leaders: "
                + "; ".join(
                    f"{x.get('symbol')}({_fmt_metric(x.get('effective_intent_score'))})"
                    for x in leaders[:5]
                )
            )
        if laggards:
            lines.append(
                "Laggards: "
                + "; ".join(
                    f"{x.get('symbol')}({_fmt_metric(x.get('effective_intent_score'))})"
                    for x in laggards[:5]
                )
            )
        lines.extend(
            [
                "",
                "--- Buckets ---",
                f"High-conviction momentum: {len(by_bucket['high-conviction-momentum'])}",
                f"Constructive watchlist: {len(by_bucket['constructive-watchlist'])}",
                f"Neutral/weak: {len(by_bucket['neutral-weak'])}",
                f"Trap-risk: {len(by_bucket['trap-risk'])}",
                "",
                "--- LLM forensic narrative ---",
            ]
        )
        lines.extend(
            [
            post.strip(),
            "",
            "--- Risk overlays ---",
            f"Cluster breadth red ratio (<= -1% day): {_fmt_metric(red_ratio * 100.0)}%",
            f"Cluster bullish downgrades applied: {cluster_downgrades}",
            f"Event-risk adjustments applied: {event_adjustments}",
            f"Macro guardrail applied: {'yes' if macro_applied else 'no'} ({macro_reason})",
            (
                "Quality checks: "
                + (", ".join(qc_warnings) if qc_warnings else "ok")
            ),
            "",
            "--- Per-symbol metrics ---",
            ]
        )
        # Rank by highest intent first so the digest starts with stronger setups.
        ranked = sorted(
            ok_results,
            key=lambda x: (
                float("-inf")
                if math.isnan(
                    _safe_float(x["audit"].get("effective_intent_score", float("nan")))
                )
                else _safe_float(x["audit"].get("effective_intent_score", float("nan")))
            ),
            reverse=True,
        )
        for r in ranked:
            lines.append(_format_symbol_metrics_line(r))
        for r in sorted(
            (x for x in results if not x.get("ok")),
            key=lambda x: (x["symbol"], x["exchange"]),
        ):
            lines.append("")
            lines.append(f"--- {r['symbol']} ({r['exchange']}) FAILED ---")
            lines.append(r.get("error", "") or "")
        digest_text = "\n".join(lines).strip()
        send_success_post_email(digest_text, subject_prefix=f"Titan V12.0 sector {sector_id}")
        print(digest_text)
        return

    lines = [f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded\n"]
    for r in sorted(results, key=lambda x: (x["symbol"], x["exchange"])):
        if r.get("ok"):
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) ---\n")
            lines.append((r.get("post") or "").strip())
        else:
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) FAILED ---\n{r.get('error', '')}\n")

    digest_out = "\n".join(lines).strip()
    send_success_post_email(digest_out, subject_prefix=f"Titan V12.0 sector {sector_id}")
    try:
        from analysis_store import persist_sector_run_analytics, update_sector_period_rollups

        ok_audits = [r["audit"] for r in results if r.get("ok") and isinstance(r.get("audit"), dict)]
        persist_sector_run_analytics(
            cfg,
            sector=sector_id,
            audits=ok_audits,
            mode="sector_per_symbol_narrative",
            ok_count=ok_count,
            total_count=len(results),
        )
        update_sector_period_rollups(cfg, sector=sector_id)
    except Exception:
        logger.exception("Analysis store persist hook failed")
    print(digest_out)
