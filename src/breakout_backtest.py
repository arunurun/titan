"""Breakout scanner historical backtest: universe, replay, forward validation, reports."""

from __future__ import annotations

import csv
import datetime
import json
import math
import time
from collections import defaultdict
from datetime import date, datetime as dt
from pathlib import Path
from typing import Any

from breakout_scanner import (
    ADX_HARD_FLOOR,
    ADX_SOFT_FLOOR,
    FILTERS,
    HOT_VOL_THRESHOLD,
    INDEX_URLS,
    PCT_CHANGE_MAX_POWER_GAP,
    PCT_CHANGE_MIN,
    RSI_MAX_HOT,
    RSI_MAX_NORMAL,
    RSI_MIN,
    SMA20_RECLAIM_VOL_THRESHOLD,
    bar_dates_from_df,
    download_nse_tickers,
    evaluate_bars_as_of,
    fetch_yahoo_history,
    warm_yahoo_session,
    _volume_filter_passes,
)

FORWARD_HORIZONS: tuple[int, ...] = (5, 10, 15)
CLOSE_RETURN_THRESHOLDS: tuple[float, ...] = (8.0, 15.0)
MFE_THRESHOLDS: tuple[float, ...] = (8.0, 15.0)
MIN_HISTORY_BARS = 50
MISSED_BREAKOUT_FORWARD_H = 15
MISSED_BREAKOUT_MIN_RETURN_PCT = 8.0
FETCH_CHUNK_SIZE = 50
FETCH_CHUNK_COOLDOWN_SEC = 120


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_output_dir() -> Path:
    return _repo_root() / "output" / "breakoutcheck"


def _parse_bhav_date(raw: str) -> str | None:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return dt.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def load_bhav_liquidity(nse_cache_dir: Path) -> dict[str, float]:
    """Average daily turnover (lacs) per EQ symbol from cached bhav copies."""
    totals: dict[str, list[float]] = defaultdict(list)
    if not nse_cache_dir.is_dir():
        return {}
    for path in sorted(nse_cache_dir.glob("sec_bhavdata_full_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.reader(f):
                    if len(row) < 12:
                        continue
                    cells = [c.strip() for c in row]
                    if cells[1] != "EQ":
                        continue
                    sym = cells[0]
                    try:
                        turnover = float(cells[11])
                    except (TypeError, ValueError):
                        continue
                    if turnover > 0:
                        totals[sym].append(turnover)
        except OSError:
            continue
    return {sym: sum(vals) / len(vals) for sym, vals in totals.items() if vals}


def build_backtest_universe(
    *,
    nse_cache_dir: Path | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """Top-N liquid small-cap + top-N liquid micro-cap from NSE index constituents."""
    cache_dir = nse_cache_dir or (_repo_root() / "temp" / "nse_cache")
    liquidity = load_bhav_liquidity(cache_dir)

    small_raw = download_nse_tickers(INDEX_URLS["SMALL_CAP_100"])
    micro_raw = download_nse_tickers(INDEX_URLS["MICRO_CAP_250"])

    def _rank_pick(tickers: list[str], tier_key: str) -> list[dict[str, Any]]:
        scored: list[tuple[str, float]] = []
        for t in tickers:
            sym = t.replace(".NS", "")
            score = liquidity.get(sym, 0.0)
            scored.append((sym, score))
        scored.sort(key=lambda x: (-x[1], x[0]))
        out: list[dict[str, Any]] = []
        for sym, score in scored[:top_n]:
            out.append({
                "symbol": sym,
                "yahoo_ticker": f"{sym}.NS",
                "tier_key": tier_key,
                "tier_label": FILTERS[tier_key]["type"],
                "liquidity_turnover_lacs_avg": round(score, 4),
                "liquidity_source": "bhav" if score > 0 else "index_order_fallback",
            })
        return out

    small = _rank_pick(small_raw, "SMALL_CAP_100")
    micro = _rank_pick(micro_raw, "MICRO_CAP_250")
    universe = small + micro
    return {
        "built_at": dt.now().isoformat(timespec="seconds"),
        "nse_cache_dir": str(cache_dir),
        "top_n_per_tier": top_n,
        "small_cap_count": len(small),
        "micro_cap_count": len(micro),
        "total": len(universe),
        "stocks": universe,
    }


def build_full_universe() -> dict[str, Any]:
    """All Nifty Smallcap 100 + Microcap 250 index constituents (no liquidity filter)."""
    small_raw = download_nse_tickers(INDEX_URLS["SMALL_CAP_100"])
    micro_raw = download_nse_tickers(INDEX_URLS["MICRO_CAP_250"])

    def _to_entries(tickers: list[str], tier_key: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in tickers:
            sym = t.replace(".NS", "")
            out.append({
                "symbol": sym,
                "yahoo_ticker": t,
                "tier_key": tier_key,
                "tier_label": FILTERS[tier_key]["type"],
                "liquidity_turnover_lacs_avg": None,
                "liquidity_source": "index_constituent",
            })
        return out

    small = _to_entries(small_raw, "SMALL_CAP_100")
    micro = _to_entries(micro_raw, "MICRO_CAP_250")
    universe = small + micro
    return {
        "built_at": dt.now().isoformat(timespec="seconds"),
        "universe_mode": "full",
        "small_cap_count": len(small),
        "micro_cap_count": len(micro),
        "total": len(universe),
        "stocks": universe,
    }


def fetch_universe_history(
    universe: list[dict[str, Any]],
    *,
    range_str: str = "6m",
    warm_session: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch Yahoo OHLCV for each universe member; return data map + manifest."""
    if warm_session:
        warm_yahoo_session()

    data_by_symbol: dict[str, dict[str, Any]] = {}
    manifest: dict[str, Any] = {
        "range": range_str,
        "fetched_at": dt.now().isoformat(timespec="seconds"),
        "success": [],
        "failed": [],
    }
    for entry in universe:
        sym = entry["symbol"]
        ticker = entry["yahoo_ticker"]
        df, err = fetch_yahoo_history(ticker, range_str=range_str, min_bars=MIN_HISTORY_BARS)
        if df and not err:
            data_by_symbol[sym] = {
                "df": df,
                "dates": bar_dates_from_df(df),
                "tier_key": entry["tier_key"],
                "tier_label": entry["tier_label"],
                "yahoo_ticker": ticker,
                "bar_count": len(df["close"]),
            }
            manifest["success"].append({
                "symbol": sym,
                "yahoo_ticker": ticker,
                "bar_count": len(df["close"]),
                "first_date": bar_dates_from_df(df)[0] if df.get("timestamp") else None,
                "last_date": bar_dates_from_df(df)[-1] if df.get("timestamp") else None,
            })
        else:
            manifest["failed"].append({
                "symbol": sym,
                "yahoo_ticker": ticker,
                "error": err or "fetch_failed",
            })
    return data_by_symbol, manifest


def fetch_universe_history_throttled(
    universe: list[dict[str, Any]],
    *,
    range_str: str = "6m",
    warm_session: bool = True,
    chunk_size: int = FETCH_CHUNK_SIZE,
    chunk_cooldown_sec: int = FETCH_CHUNK_COOLDOWN_SEC,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch Yahoo OHLCV with scanner-style chunk cool-down between batches."""
    if warm_session:
        warm_yahoo_session()

    data_by_symbol: dict[str, dict[str, Any]] = {}
    manifest: dict[str, Any] = {
        "range": range_str,
        "fetched_at": dt.now().isoformat(timespec="seconds"),
        "chunk_size": chunk_size,
        "chunk_cooldown_sec": chunk_cooldown_sec,
        "success": [],
        "failed": [],
    }
    for i, entry in enumerate(universe, start=1):
        sym = entry["symbol"]
        ticker = entry["yahoo_ticker"]
        df, err = fetch_yahoo_history(ticker, range_str=range_str, min_bars=MIN_HISTORY_BARS)
        if df and not err:
            data_by_symbol[sym] = {
                "df": df,
                "dates": bar_dates_from_df(df),
                "tier_key": entry["tier_key"],
                "tier_label": entry["tier_label"],
                "yahoo_ticker": ticker,
                "bar_count": len(df["close"]),
            }
            manifest["success"].append({
                "symbol": sym,
                "yahoo_ticker": ticker,
                "bar_count": len(df["close"]),
                "first_date": bar_dates_from_df(df)[0] if df.get("timestamp") else None,
                "last_date": bar_dates_from_df(df)[-1] if df.get("timestamp") else None,
            })
        else:
            manifest["failed"].append({
                "symbol": sym,
                "yahoo_ticker": ticker,
                "error": err or "fetch_failed",
            })
        if i % chunk_size == 0 and i < len(universe):
            print(
                f"Chunk cool-down: {i}/{len(universe)} tickers fetched, "
                f"sleeping {chunk_cooldown_sec}s...",
                flush=True,
            )
            time.sleep(chunk_cooldown_sec)
    return data_by_symbol, manifest


def _all_filter_failures(eval_result: dict[str, Any], tier_key: str) -> list[str]:
    """Return every production filter that fails on this bar (not just first)."""
    filt = FILTERS[tier_key]
    failures: list[str] = []
    price = eval_result.get("latest_price", 0.0)
    if price < filt["min_price"]:
        failures.append("min_price")

    pct = eval_result.get("pct_change", 0.0)
    if pct < PCT_CHANGE_MIN or pct > PCT_CHANGE_MAX_POWER_GAP:
        failures.append("pct_change")

    sma50 = eval_result.get("sma50_last", 0.0)
    sma20 = eval_result.get("sma20_last", 0.0)
    vol_mult = eval_result.get("vol_mult", 0.0)
    if price < sma50:
        sma20_reclaim = price >= sma20 and vol_mult >= SMA20_RECLAIM_VOL_THRESHOLD
        if not sma20_reclaim:
            failures.append("SMA50")

    vol_cum = eval_result.get("vol_cum_mult", 0.0)
    prior_spike = eval_result.get("prior_volume_spike", False)
    vol_ok, _ = _volume_filter_passes(
        vol_mult=vol_mult,
        vol_cum_mult=vol_cum,
        vol_thresh=filt["vol_mult"],
        tier_name=tier_key,
        prior_spike=prior_spike,
    )
    if not vol_ok:
        failures.append("vol")

    rsi = eval_result.get("rsi_val", 0.0)
    rsi_ok = RSI_MIN <= rsi <= RSI_MAX_NORMAL
    if not rsi_ok and RSI_MAX_NORMAL < rsi <= RSI_MAX_HOT and vol_mult > HOT_VOL_THRESHOLD:
        rsi_ok = True
    if not rsi_ok:
        failures.append("RSI")

    adx = eval_result.get("adx_val", 0.0)
    adx_ok = adx >= ADX_HARD_FLOOR
    if (
        not adx_ok
        and ADX_SOFT_FLOOR <= adx < ADX_HARD_FLOOR
        and vol_mult >= filt["vol_mult"]
        and price > sma50
        and pct > 0
    ):
        adx_ok = True
    if not adx_ok:
        failures.append("ADX")

    if eval_result.get("target_gain", 0.0) < 8.0:
        failures.append("target_gain")
    return failures


def _forward_max_return_pct(df: dict[str, Any], signal_idx: int, horizon: int) -> float | None:
    """Max close-to-close gain from signal close within next `horizon` sessions."""
    closes = df["close"]
    n = len(closes)
    end = min(signal_idx + horizon, n - 1)
    if signal_idx >= n - 1:
        return None
    entry = closes[signal_idx]
    if entry <= 0:
        return None
    max_ret = 0.0
    for j in range(signal_idx + 1, end + 1):
        ret = (closes[j] / entry - 1.0) * 100.0
        max_ret = max(max_ret, ret)
    return round(max_ret, 4)


def analyze_missed_breakouts_for_stock(
    sym: str,
    stock_data: dict[str, Any],
    *,
    forward_h: int = MISSED_BREAKOUT_FORWARD_H,
    min_return_pct: float = MISSED_BREAKOUT_MIN_RETURN_PCT,
) -> dict[str, Any]:
    """Days with strong forward returns that did not produce a PASS signal."""
    df = stock_data["df"]
    dates = stock_data["dates"]
    tier_key = stock_data["tier_key"]
    n = len(df["close"])

    missed: list[dict[str, Any]] = []
    single_filter_near_misses: list[dict[str, Any]] = []
    obvious_blocked: list[dict[str, Any]] = []

    for idx in range(MIN_HISTORY_BARS - 1, n):
        eval_result = evaluate_bars_as_of(df, idx, tier_key)
        if eval_result.get("passed"):
            continue

        fwd_max = _forward_max_return_pct(df, idx, forward_h)
        if fwd_max is None or fwd_max < min_return_pct:
            continue

        all_fails = _all_filter_failures(eval_result, tier_key)
        primary_fail = eval_result.get("fail_reason")
        row = {
            "signal_date": dates[idx] if idx < len(dates) else "",
            "bar_idx": idx,
            "primary_fail_reason": primary_fail,
            "all_fail_reasons": all_fails,
            "fail_count": len(all_fails),
            "forward_max_return_pct": fwd_max,
            "pct_change": eval_result.get("pct_change"),
            "vol_mult": eval_result.get("vol_mult"),
            "rsi_val": eval_result.get("rsi_val"),
            "adx_val": eval_result.get("adx_val"),
            "latest_price": eval_result.get("latest_price"),
        }
        missed.append(row)
        if len(all_fails) == 1:
            single_filter_near_misses.append(row)
        pct = eval_result.get("pct_change", 0.0) or 0.0
        vol = eval_result.get("vol_mult", 0.0) or 0.0
        if pct >= 10.0 and vol >= FILTERS[tier_key]["vol_mult"]:
            obvious_blocked.append(row)

    fail_counts: dict[str, int] = defaultdict(int)
    single_fail_counts: dict[str, int] = defaultdict(int)
    for m in missed:
        fail_counts[m["primary_fail_reason"] or "unknown"] += 1
    for m in single_filter_near_misses:
        reasons = m.get("all_fail_reasons") or []
        if len(reasons) == 1:
            single_fail_counts[reasons[0]] += 1

    return {
        "symbol": sym,
        "tier_label": stock_data["tier_label"],
        "missed_count": len(missed),
        "single_filter_near_miss_count": len(single_filter_near_misses),
        "obvious_blocked_count": len(obvious_blocked),
        "primary_fail_reason_counts": dict(sorted(fail_counts.items(), key=lambda x: -x[1])),
        "single_filter_fail_counts": dict(sorted(single_fail_counts.items(), key=lambda x: -x[1])),
        "missed_opportunities": missed,
        "single_filter_near_misses": single_filter_near_misses,
        "obvious_blocked": obvious_blocked,
    }


def analyze_missed_breakouts(
    data_by_symbol: dict[str, dict[str, Any]],
    *,
    forward_h: int = MISSED_BREAKOUT_FORWARD_H,
    min_return_pct: float = MISSED_BREAKOUT_MIN_RETURN_PCT,
) -> dict[str, Any]:
    """Cohort missed-breakout analysis across a fetched universe."""
    per_stock: dict[str, Any] = {}
    cohort_primary: dict[str, int] = defaultdict(int)
    cohort_single: dict[str, int] = defaultdict(int)
    total_missed = 0
    total_single = 0
    total_obvious = 0

    for sym, stock_data in sorted(data_by_symbol.items()):
        stock_report = analyze_missed_breakouts_for_stock(
            sym, stock_data, forward_h=forward_h, min_return_pct=min_return_pct,
        )
        per_stock[sym] = stock_report
        total_missed += stock_report["missed_count"]
        total_single += stock_report["single_filter_near_miss_count"]
        total_obvious += stock_report["obvious_blocked_count"]
        for reason, cnt in (stock_report.get("primary_fail_reason_counts") or {}).items():
            cohort_primary[reason] += cnt
        for reason, cnt in (stock_report.get("single_filter_fail_counts") or {}).items():
            cohort_single[reason] += cnt

    return {
        "forward_horizon_sessions": forward_h,
        "min_forward_return_pct": min_return_pct,
        "stocks_analyzed": len(per_stock),
        "total_missed_opportunities": total_missed,
        "total_single_filter_near_misses": total_single,
        "total_obvious_blocked": total_obvious,
        "cohort_primary_fail_counts": dict(sorted(cohort_primary.items(), key=lambda x: -x[1])),
        "cohort_single_filter_fail_counts": dict(sorted(cohort_single.items(), key=lambda x: -x[1])),
        "per_stock": per_stock,
    }


def build_missed_breakouts_markdown(analysis: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Missed Breakouts Analysis (40-Stock Backtest)",
        "",
        f"- **Forward horizon**: T+{analysis.get('forward_horizon_sessions')} sessions",
        f"- **Miss threshold**: +{analysis.get('min_forward_return_pct')}% max close return without PASS",
        f"- **Stocks analyzed**: {analysis.get('stocks_analyzed')}",
        "",
        "## Cohort Summary",
        "",
        f"| Metric | Count |",
        f"| :--- | ---: |",
        f"| Total missed opportunities | {analysis.get('total_missed_opportunities', 0)} |",
        f"| Single-filter near misses | {analysis.get('total_single_filter_near_misses', 0)} |",
        f"| Obvious breakouts blocked (+10% day, vol OK) | {analysis.get('total_obvious_blocked', 0)} |",
        "",
        "### Primary blocking filter (first sequential fail)",
        "",
    ]
    for reason, cnt in (analysis.get("cohort_primary_fail_counts") or {}).items():
        lines.append(f"- **{reason}**: {cnt}")
    lines.extend(["", "### Single-filter near misses (would pass if one rule removed)", ""])
    for reason, cnt in (analysis.get("cohort_single_filter_fail_counts") or {}).items():
        lines.append(f"- **{reason}**: {cnt}")

    lines.extend(["", "## Per-Stock Results", ""])
    for sym in sorted((analysis.get("per_stock") or {}).keys()):
        stock = analysis["per_stock"][sym]
        lines.append(f"### {sym} ({stock.get('tier_label', 'n/a')})")
        lines.append(
            f"- Missed: {stock.get('missed_count', 0)} | "
            f"Single-filter near misses: {stock.get('single_filter_near_miss_count', 0)} | "
            f"Obvious blocked: {stock.get('obvious_blocked_count', 0)}"
        )
        prim = stock.get("primary_fail_reason_counts") or {}
        if prim:
            lines.append(f"- Primary blocks: {', '.join(f'{k}={v}' for k, v in prim.items())}")
        singles = stock.get("single_filter_near_misses") or []
        if singles:
            lines.append("- **Near misses (1 filter only)**:")
            for m in singles[:5]:
                reasons = m.get("all_fail_reasons") or []
                lines.append(
                    f"  - {m.get('signal_date')}: fwd +{m.get('forward_max_return_pct')}% | "
                    f"blocked by {reasons[0] if reasons else '?'} | "
                    f"chg {m.get('pct_change')}% vol {m.get('vol_mult')}x"
                )
            if len(singles) > 5:
                lines.append(f"  - ... and {len(singles) - 5} more")
        obvious = stock.get("obvious_blocked") or []
        if obvious:
            lines.append("- **Obvious breakouts blocked**:")
            for m in obvious[:3]:
                lines.append(
                    f"  - {m.get('signal_date')}: +{m.get('pct_change')}% / "
                    f"vol {m.get('vol_mult')}x → blocked by {m.get('primary_fail_reason')}"
                )
        lines.append("")

    lines.extend([
        "",
        "## Methodology",
        "",
        "Replays production filters point-in-time on cached Yahoo 6m bars. "
        "A missed opportunity is a day that failed filters but achieved "
        f"+{analysis.get('min_forward_return_pct')}% or more within "
        f"T+{analysis.get('forward_horizon_sessions')} sessions (close-to-close max). "
        "Primary fail reason follows production sequential filter order. "
        "Single-filter near miss = exactly one filter fails when all are checked independently.",
        "",
        "*Analysis only; production thresholds unchanged.*",
    ])
    return "\n".join(lines)


def _path_outcome_for_bar(
    *,
    open_p: float,
    high: float,
    low: float,
    stop: float,
    target: float,
) -> str | None:
    """Intraday path: stop checked before target on same bar (conservative)."""
    stop_hit = low <= stop
    target_hit = high >= target
    if stop_hit and target_hit:
        # Open-distance heuristic when both levels trade on same bar.
        dist_stop = abs(open_p - stop)
        dist_target = abs(target - open_p)
        if dist_target < dist_stop:
            return "win"
        if dist_stop < dist_target:
            return "loss"
        return "ambiguous_same_bar"
    if stop_hit:
        return "loss"
    if target_hit:
        return "win"
    return None


def validate_forward_path(
    df: dict[str, Any],
    signal_idx: int,
    *,
    entry: float,
    stop: float,
    target: float,
    horizons: tuple[int, ...] = FORWARD_HORIZONS,
) -> dict[str, Any]:
    """Forward validation: target before stop = win; report MFE/MAE per horizon."""
    max_h = max(horizons)
    n = len(df["close"])
    forward_end = min(signal_idx + max_h, n - 1)
    sessions_available = max(0, forward_end - signal_idx)

    mfe_pct = 0.0
    mae_pct = 0.0
    first_exit: str | None = None
    first_exit_bar: int | None = None
    first_exit_reason: str | None = None

    for j in range(signal_idx + 1, forward_end + 1):
        hi = df["high"][j]
        lo = df["low"][j]
        op = df["open"][j]
        mfe_pct = max(mfe_pct, (hi / entry - 1.0) * 100.0)
        mae_pct = min(mae_pct, (lo / entry - 1.0) * 100.0)

        bar_outcome = _path_outcome_for_bar(
            open_p=op, high=hi, low=lo, stop=stop, target=target,
        )
        if bar_outcome in ("win", "loss"):
            first_exit = bar_outcome
            first_exit_bar = j - signal_idx
            first_exit_reason = "target_hit" if bar_outcome == "win" else "stop_hit"
            break
        if bar_outcome == "ambiguous_same_bar":
            first_exit = "ambiguous"
            first_exit_bar = j - signal_idx
            first_exit_reason = "both_levels_same_bar"

    horizon_results: dict[str, Any] = {}
    for h in horizons:
        end_idx = signal_idx + h
        if end_idx >= n:
            horizon_results[f"t{h}"] = {
                "sessions_available": max(0, n - 1 - signal_idx),
                "result": "insufficient_bars",
                "win": None,
            }
            continue

        h_outcome: str | None = None
        h_reason: str | None = None
        for j in range(signal_idx + 1, end_idx + 1):
            bar_outcome = _path_outcome_for_bar(
                open_p=df["open"][j],
                high=df["high"][j],
                low=df["low"][j],
                stop=stop,
                target=target,
            )
            if bar_outcome == "win":
                h_outcome = "win"
                h_reason = "target_hit"
                break
            if bar_outcome == "loss":
                h_outcome = "loss"
                h_reason = "stop_hit"
                break
            if bar_outcome == "ambiguous_same_bar":
                h_outcome = "ambiguous"
                h_reason = "both_levels_same_bar"
                break

        if h_outcome is None:
            close_h = df["close"][end_idx]
            ret_pct = (close_h / entry - 1.0) * 100.0
            h_outcome = "open"
            h_reason = "neither_hit_timeout"

        horizon_results[f"t{h}"] = {
            "sessions_available": h,
            "result": h_outcome,
            "win": h_outcome == "win",
            "reason": h_reason,
        }

    mismatch_reason = _classify_mismatch(first_exit, first_exit_reason, sessions_available, max_h)

    close_returns: dict[str, float | None] = {}
    for h in horizons:
        end_idx = signal_idx + h
        if end_idx < n:
            close_returns[f"t{h}"] = round((df["close"][end_idx] / entry - 1.0) * 100.0, 4)
        else:
            close_returns[f"t{h}"] = None

    max_close_return = max(
        (v for v in close_returns.values() if v is not None),
        default=None,
    )
    efficacy = {
        "mfe_hit_8": mfe_pct >= MFE_THRESHOLDS[0],
        "mfe_hit_15": mfe_pct >= MFE_THRESHOLDS[1],
        "close_hit_8pct": max_close_return is not None and max_close_return >= CLOSE_RETURN_THRESHOLDS[0],
        "close_hit_15pct": max_close_return is not None and max_close_return >= CLOSE_RETURN_THRESHOLDS[1],
        "max_close_return_pct": round(max_close_return, 4) if max_close_return is not None else None,
        "close_returns": close_returns,
    }

    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "sessions_available": sessions_available,
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "first_exit": first_exit,
        "first_exit_bar": first_exit_bar,
        "first_exit_reason": first_exit_reason,
        "horizons": horizon_results,
        "mismatch_reason": mismatch_reason,
        "efficacy": efficacy,
    }


def _classify_mismatch(
    first_exit: str | None,
    first_exit_reason: str | None,
    sessions_available: int,
    max_h: int,
) -> str | None:
    if sessions_available < max_h:
        return "insufficient_forward_bars"
    if first_exit == "win":
        return None
    if first_exit == "loss":
        return "stopped_out_before_target"
    if first_exit == "ambiguous":
        return "both_levels_same_bar"
    return "neither_hit_within_window"


def replay_stock(
    sym: str,
    stock_data: dict[str, Any],
) -> dict[str, Any]:
    """Daily replay of production filters; forward-validate each passing signal."""
    df = stock_data["df"]
    dates = stock_data["dates"]
    tier_key = stock_data["tier_key"]
    n = len(df["close"])

    evaluations: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    watch_signals: list[dict[str, Any]] = []
    last_pass_idx: int | None = None

    for idx in range(MIN_HISTORY_BARS - 1, n):
        eval_result = evaluate_bars_as_of(df, idx, tier_key, last_pass_idx=last_pass_idx)
        signal_date = dates[idx] if idx < len(dates) else ""
        row = {
            "signal_date": signal_date,
            "bar_idx": idx,
            "prediction": {
                "passed": eval_result["passed"],
                "signal_tier": eval_result.get("signal_tier"),
                "fail_reason": eval_result.get("fail_reason"),
                "metrics": {k: eval_result[k] for k in eval_result if k not in ("passed", "fail_reason", "signal_tier")},
            },
        }
        evaluations.append(row)
        if eval_result["passed"]:
            last_pass_idx = idx
            outcome = validate_forward_path(
                df,
                idx,
                entry=eval_result["latest_price"],
                stop=eval_result["sl_price"],
                target=eval_result["target_price"],
            )
            signals.append({
                **row,
                "outcome": outcome,
            })
        elif eval_result.get("signal_tier") == "WATCH":
            outcome = validate_forward_path(
                df,
                idx,
                entry=eval_result["latest_price"],
                stop=eval_result["sl_price"],
                target=eval_result["target_price"],
            )
            watch_signals.append({
                **row,
                "outcome": outcome,
            })

    return {
        "symbol": sym,
        "tier_key": tier_key,
        "tier_label": stock_data["tier_label"],
        "yahoo_ticker": stock_data["yahoo_ticker"],
        "bar_count": n,
        "date_range": {
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
        },
        "evaluation_days": len(evaluations),
        "signal_count": len(signals),
        "watch_signal_count": len(watch_signals),
        "signals": signals,
        "watch_signals": watch_signals,
        "near_miss_top_failures": _top_fail_reasons(evaluations),
    }


def _top_fail_reasons(evaluations: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for ev in evaluations:
        pred = ev.get("prediction") or {}
        if not pred.get("passed"):
            reason = pred.get("fail_reason") or "unknown"
            counts[reason] += 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [{"fail_reason": r, "count": c} for r, c in ranked[:top_k]]


def _aggregate_hit_rates(all_signals: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total_signals": len(all_signals)}
    for h in FORWARD_HORIZONS:
        key = f"t{h}"
        wins = 0
        coverage = 0
        for sig in all_signals:
            outcome = sig.get("outcome") or {}
            hz = (outcome.get("horizons") or {}).get(key) or {}
            if hz.get("win") is True:
                wins += 1
                coverage += 1
            elif hz.get("win") is False:
                coverage += 1
            elif hz.get("result") not in (None, "insufficient_bars"):
                coverage += 1
        summary[f"hit_rate_{key}"] = round(100.0 * wins / coverage, 2) if coverage else None
        summary[f"coverage_{key}"] = coverage
        summary[f"wins_{key}"] = wins

    # Decoupled efficacy: MFE and close-return thresholds (primary validation)
    n = len(all_signals)
    if n:
        eff = [(sig.get("outcome") or {}).get("efficacy") or {} for sig in all_signals]
        summary["mfe_hit_8_count"] = sum(1 for e in eff if e.get("mfe_hit_8"))
        summary["mfe_hit_15_count"] = sum(1 for e in eff if e.get("mfe_hit_15"))
        summary["close_hit_8pct_count"] = sum(1 for e in eff if e.get("close_hit_8pct"))
        summary["close_hit_15pct_count"] = sum(1 for e in eff if e.get("close_hit_15pct"))
        summary["mfe_hit_8_rate"] = round(100.0 * summary["mfe_hit_8_count"] / n, 2)
        summary["mfe_hit_15_rate"] = round(100.0 * summary["mfe_hit_15_count"] / n, 2)
        summary["close_hit_8pct_rate"] = round(100.0 * summary["close_hit_8pct_count"] / n, 2)
        summary["close_hit_15pct_rate"] = round(100.0 * summary["close_hit_15pct_count"] / n, 2)
    else:
        for key in (
            "mfe_hit_8_count", "mfe_hit_15_count", "close_hit_8pct_count",
            "close_hit_15pct_count", "mfe_hit_8_rate", "mfe_hit_15_rate",
            "close_hit_8pct_rate", "close_hit_15pct_rate",
        ):
            summary[key] = 0 if key.endswith("_count") else None

    mismatch_counts: dict[str, int] = defaultdict(int)
    for sig in all_signals:
        reason = (sig.get("outcome") or {}).get("mismatch_reason")
        if reason:
            mismatch_counts[reason] += 1
    summary["mismatch_taxonomy"] = dict(sorted(mismatch_counts.items(), key=lambda x: -x[1]))
    return summary


def load_cached_universe_history(
    universe: list[dict[str, Any]],
    *,
    range_str: str = "6m",
) -> dict[str, dict[str, Any]]:
    """Load OHLCV from backtest Yahoo cache only (no network)."""
    data_by_symbol: dict[str, dict[str, Any]] = {}
    for entry in universe:
        sym = entry["symbol"]
        ticker = entry["yahoo_ticker"]
        df, err = fetch_yahoo_history(ticker, range_str=range_str, min_bars=MIN_HISTORY_BARS)
        if df and not err:
            data_by_symbol[sym] = {
                "df": df,
                "dates": bar_dates_from_df(df),
                "tier_key": entry["tier_key"],
                "tier_label": entry["tier_label"],
                "yahoo_ticker": ticker,
                "bar_count": len(df["close"]),
            }
    return data_by_symbol


def run_missed_breakout_analysis(
    *,
    universe: list[dict[str, Any]] | None = None,
    nse_cache_dir: Path | None = None,
    top_n: int = 20,
    range_str: str = "6m",
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Analyze missed breakouts using cached Yahoo history (no refetch)."""
    out_dir = output_dir or default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    if universe is None:
        universe = build_backtest_universe(nse_cache_dir=nse_cache_dir, top_n=top_n)["stocks"]

    data_by_symbol = load_cached_universe_history(universe, range_str=range_str)
    analysis = analyze_missed_breakouts(data_by_symbol)
    md = build_missed_breakouts_markdown(analysis)
    json_path = out_dir / "missed_breakouts_analysis.json"
    md_path = out_dir / "missed_breakouts_analysis.md"
    json_path.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    analysis["paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return analysis


def run_backtest(
    *,
    universe: list[dict[str, Any]] | None = None,
    nse_cache_dir: Path | None = None,
    top_n: int = 20,
    range_str: str = "6m",
    output_dir: Path | None = None,
    warm_session: bool = True,
    stock_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Full pipeline: universe -> fetch -> replay -> report artifacts."""
    started = dt.now()
    out_dir = output_dir or default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    if universe is None:
        universe_payload = build_backtest_universe(nse_cache_dir=nse_cache_dir, top_n=top_n)
        universe = universe_payload["stocks"]
    else:
        small_n = sum(1 for u in universe if u.get("tier_key") == "SMALL_CAP_100")
        micro_n = sum(1 for u in universe if u.get("tier_key") == "MICRO_CAP_250")
        universe_payload = {
            "built_at": started.isoformat(timespec="seconds"),
            "stocks": universe,
            "total": len(universe),
            "small_cap_count": small_n,
            "micro_cap_count": micro_n,
        }

    if stock_filter:
        wanted = {s.strip().upper() for s in stock_filter}
        universe = [u for u in universe if u["symbol"] in wanted]

    if len(universe) > FETCH_CHUNK_SIZE:
        data_by_symbol, fetch_manifest = fetch_universe_history_throttled(
            universe, range_str=range_str, warm_session=warm_session,
        )
    else:
        data_by_symbol, fetch_manifest = fetch_universe_history(
            universe, range_str=range_str, warm_session=warm_session,
        )

    per_stock: dict[str, Any] = {}
    all_signals: list[dict[str, Any]] = []
    for entry in universe:
        sym = entry["symbol"]
        if sym not in data_by_symbol:
            per_stock[sym] = {
                "symbol": sym,
                "tier_label": entry.get("tier_label"),
                "error": "fetch_failed",
                "signal_count": 0,
                "signals": [],
            }
            continue
        stock_report = replay_stock(sym, data_by_symbol[sym])
        per_stock[sym] = stock_report
        for sig in stock_report.get("signals") or []:
            all_signals.append({**sig, "symbol": sym, "tier_label": entry.get("tier_label")})

    finished = dt.now()
    summary = _aggregate_hit_rates(all_signals)
    summary["stocks_fetched"] = len(fetch_manifest.get("success") or [])
    summary["stocks_failed"] = len(fetch_manifest.get("failed") or [])
    summary["stocks_replayed"] = len([s for s in per_stock.values() if not s.get("error")])

    report = {
        "meta": {
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_sec": round((finished - started).total_seconds(), 2),
            "range": range_str,
            "forward_horizons": list(FORWARD_HORIZONS),
            "min_history_bars": MIN_HISTORY_BARS,
            "output_dir": str(out_dir),
        },
        "universe_summary": {
            "total": universe_payload.get("total", len(universe)),
            "small_cap_count": universe_payload.get("small_cap_count"),
            "micro_cap_count": universe_payload.get("micro_cap_count"),
        },
        "summary": summary,
        "fetch_manifest": fetch_manifest,
        "per_stock": per_stock,
    }

    universe_path = out_dir / "universe_40.json"
    manifest_path = out_dir / "fetch_manifest.json"
    json_path = out_dir / "breakout_backtest_report.json"
    md_path = out_dir / "breakout_backtest_report.md"

    universe_path.write_text(json.dumps(universe_payload, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(fetch_manifest, indent=2), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(build_report_markdown(report), encoding="utf-8")

    report["paths"] = {
        "json": str(json_path),
        "markdown": str(md_path),
        "universe": str(universe_path),
        "fetch_manifest": str(manifest_path),
    }
    return report


def build_report_markdown(report: dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    summary = report.get("summary") or {}
    lines: list[str] = [
        "# Breakout Scanner Backtest Report",
        "",
        f"- **Run window**: Yahoo `{meta.get('range', '6m')}` daily bars",
        f"- **Started**: {meta.get('started_at')}",
        f"- **Finished**: {meta.get('finished_at')} ({meta.get('duration_sec')}s)",
        f"- **Forward horizons**: T+{', T+'.join(str(h) for h in meta.get('forward_horizons', FORWARD_HORIZONS))}",
        "",
        "## Cohort Summary",
        "",
        f"| Metric | Value |",
        f"| :--- | ---: |",
        f"| Total signals | {summary.get('total_signals', 0)} |",
    ]
    for h in FORWARD_HORIZONS:
        key = f"t{h}"
        hr = summary.get(f"hit_rate_{key}")
        cov = summary.get(f"coverage_{key}")
        wins = summary.get(f"wins_{key}")
        hr_s = f"{hr}%" if hr is not None else "n/a"
        lines.append(f"| Hit rate T+{h} target ({wins}/{cov}) | {hr_s} |")
    lines.append(
        f"| MFE ≥8% / ≥15% ({summary.get('mfe_hit_8_count', 0)}/{summary.get('mfe_hit_15_count', 0)} "
        f"of {summary.get('total_signals', 0)}) | "
        f"{summary.get('mfe_hit_8_rate', 'n/a')}% / {summary.get('mfe_hit_15_rate', 'n/a')}% |"
    )
    lines.append(
        f"| Close return ≥8% / ≥15% ({summary.get('close_hit_8pct_count', 0)}/"
        f"{summary.get('close_hit_15pct_count', 0)} of {summary.get('total_signals', 0)}) | "
        f"{summary.get('close_hit_8pct_rate', 'n/a')}% / {summary.get('close_hit_15pct_rate', 'n/a')}% |"
    )
    lines.append(f"| Stocks fetched | {summary.get('stocks_fetched', 0)} |")
    lines.append(f"| Fetch failures | {summary.get('stocks_failed', 0)} |")

    taxonomy = summary.get("mismatch_taxonomy") or {}
    if taxonomy:
        lines.extend(["", "## Mismatch Taxonomy", ""])
        for reason, count in taxonomy.items():
            lines.append(f"- **{reason}**: {count}")

    failed = (report.get("fetch_manifest") or {}).get("failed") or []
    if failed:
        lines.extend(["", "## Fetch Failures", ""])
        for f in failed:
            lines.append(f"- {f.get('symbol')}: {f.get('error')}")

    lines.extend(["", "## Per-Stock Results", ""])
    for sym in sorted((report.get("per_stock") or {}).keys()):
        stock = report["per_stock"][sym]
        lines.append(f"### {sym} ({stock.get('tier_label', 'n/a')})")
        if stock.get("error"):
            lines.append(f"- **Error**: {stock['error']}")
            lines.append("")
            continue
        dr = stock.get("date_range") or {}
        lines.append(
            f"- **Bars**: {stock.get('bar_count')} ({dr.get('first')} .. {dr.get('last')})"
        )
        lines.append(f"- **Evaluation days**: {stock.get('evaluation_days')}")
        lines.append(f"- **Signals**: {stock.get('signal_count')}")
        near = stock.get("near_miss_top_failures") or []
        if near:
            fail_s = ", ".join(f"{x['fail_reason']}={x['count']}" for x in near)
            lines.append(f"- **Top filter failures**: {fail_s}")

        for sig in stock.get("signals") or []:
            pred = sig.get("prediction") or {}
            metrics = pred.get("metrics") or {}
            outcome = sig.get("outcome") or {}
            lines.append("")
            lines.append(f"#### Signal {sig.get('signal_date')}")
            lines.append(
                f"- **Prediction**: PASS @ {metrics.get('latest_price')} "
                f"(chg {metrics.get('pct_change')}%, vol {metrics.get('vol_mult')}x, "
                f"RSI {metrics.get('rsi_val')}, ADX {metrics.get('adx_val')})"
            )
            lines.append(
                f"- **Levels**: stop {metrics.get('sl_price')} / target {metrics.get('target_price')} "
                f"(gain {metrics.get('target_gain')}%)"
            )
            hz_parts = []
            for h in FORWARD_HORIZONS:
                hz = (outcome.get("horizons") or {}).get(f"t{h}") or {}
                res = hz.get("result", "n/a")
                hz_parts.append(f"T+{h}={res}")
            lines.append(
                f"- **Outcome**: {', '.join(hz_parts)}; MFE {outcome.get('mfe_pct')}% / "
                f"MAE {outcome.get('mae_pct')}%"
            )
            eff = outcome.get("efficacy") or {}
            if eff:
                lines.append(
                    f"- **Efficacy**: MFE≥8%={eff.get('mfe_hit_8')} MFE≥15%={eff.get('mfe_hit_15')} | "
                    f"close≥8%={eff.get('close_hit_8pct')} close≥15%={eff.get('close_hit_15pct')} "
                    f"(max close {eff.get('max_close_return_pct')}%)"
                )
            mm = outcome.get("mismatch_reason")
            if mm:
                lines.append(f"- **Mismatch reason**: {mm}")
        lines.append("")

    lines.extend([
        "",
        "## Methodology",
        "",
        "Daily replay applies production breakout filters point-in-time (no look-ahead). "
        "Forward validation walks T+1..T+N sessions; target hit before stop = win. "
        "Same-bar ambiguity uses open-distance heuristic.",
        "",
        "*Educational backtest only; not investment advice.*",
    ])
    return "\n".join(lines)
