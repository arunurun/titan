#!/usr/bin/env python3
"""Analyze T-15..T-1 pre-signal patterns for v2 PASS breakout signals."""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from breakout_scanner import (  # noqa: E402
    _parse_yahoo_chart_response,
    calculate_adx,
    calculate_rsi,
    calculate_sma,
)

OUT_DIR = ROOT / "output" / "breakoutcheck"
CACHE_DIR = ROOT / "data" / "cache" / "breakout_yahoo" / "backtest"
T20_JSON = OUT_DIR / "v2_pass_signals_t20_daily_report.json"
MIN_BARS = 50


def load_backtest_bars(ticker: str, range_str: str = "6m") -> dict[str, Any] | None:
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = CACHE_DIR / f"{safe}_{range_str}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _parse_yahoo_chart_response(data)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, OSError):
        return None


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def _mean(vals: list[float]) -> float | None:
    return statistics.mean(vals) if vals else None


def classify_outcome(sig: dict[str, Any]) -> dict[str, Any]:
    """Classify signal using T+20 daily path criteria."""
    dip = sig["dip_summary"]
    daily = sig["daily_path"]

    t15_row = next((r for r in daily if r.get("day") == 15), None)
    t15_pct = t15_row.get("vs_entry_pct") if t15_row else None

    max_dd = dip.get("max_drawdown_vs_entry_pct")
    ever_below = bool(dip.get("ever_below_entry"))

    below_days = sum(1 for r in daily if r.get("below_entry") is True)
    total_days = sum(1 for r in daily if not r.get("insufficient_bars"))
    underwater_pct = (below_days / total_days * 100.0) if total_days else 0.0
    underwater_most = below_days > total_days / 2 if total_days else False

    winner_reasons: list[str] = []
    failure_reasons: list[str] = []

    if not ever_below:
        winner_reasons.append("never_below_entry")
    if t15_pct is not None and t15_pct >= 8.0:
        winner_reasons.append(f"t15_ge_8pct ({t15_pct:+.2f}%)")

    if max_dd is not None and max_dd < -10.0:
        failure_reasons.append(f"max_dd_lt_-10 ({max_dd:+.2f}%)")
    if t15_pct is not None and t15_pct < 0.0:
        failure_reasons.append(f"t15_lt_0 ({t15_pct:+.2f}%)")
    if underwater_most:
        failure_reasons.append(f"underwater_most_t20 ({below_days}/{total_days} days)")

    is_winner = bool(winner_reasons)
    is_failure = bool(failure_reasons)

    if is_winner and not is_failure:
        label = "winner"
    elif is_failure and not is_winner:
        label = "failure"
    elif is_winner and is_failure:
        label = "winner"  # strong T+15 / never-below overrides dip noise
    else:
        label = "mixed"

    return {
        "label": label,
        "is_winner": is_winner,
        "is_failure": is_failure,
        "winner_reasons": winner_reasons,
        "failure_reasons": failure_reasons,
        "t15_vs_entry_pct": t15_pct,
        "max_drawdown_pct": max_dd,
        "ever_below_entry": ever_below,
        "underwater_days": below_days,
        "underwater_pct": round(underwater_pct, 1),
        "final_t20_pct": dip.get("final_t20_vs_entry_pct"),
    }


def compute_pre_signal_features(df: dict[str, Any], bar_idx: int) -> dict[str, Any] | None:
    """Compute T-15..T-1 features relative to signal bar_idx (day T)."""
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]
    n = len(closes)

    t = bar_idx
    if t < MIN_BARS or t >= n:
        return None

    # Windows (inclusive indices)
    w15_start, w15_end = t - 15, t - 1
    w10_start = t - 10
    w5_start = t - 5
    spike_end = t - 5

    if w15_start < 1:
        return None

    sma50 = calculate_sma(closes, 50)
    vol20 = calculate_sma(volumes, 20)
    rsi = calculate_rsi(closes, 14)
    adx_arr, _, _ = calculate_adx(highs, lows, closes, 14)

    close_t = closes[t]
    close_t1 = closes[t - 1]
    close_t15 = closes[t - 15]

    cum_ret_15 = _pct(close_t1, close_t15)
    cum_ret_10 = _pct(close_t1, closes[t - 10])
    cum_ret_5 = _pct(close_t1, closes[t - 5])

    window_closes = closes[w15_start : w15_end + 1]
    max_close = max(window_closes)
    min_close = min(window_closes)
    max_close_vs_t = _pct(max_close, close_t)
    min_close_vs_t = _pct(min_close, close_t)

    vol_last5 = volumes[w5_start : w15_end + 1]
    vol_prior10 = volumes[w15_start : t - 5]
    avg_vol_last5 = _mean([float(v) for v in vol_last5])
    avg_vol_prior10 = _mean([float(v) for v in vol_prior10])
    vol_trend = (
        avg_vol_last5 / avg_vol_prior10
        if avg_vol_last5 and avg_vol_prior10 and avg_vol_prior10 > 0
        else None
    )

    vol_spike_days = 0
    for i in range(w15_start, w15_end + 1):
        vma = vol20[i] if i < len(vol20) else 0.0
        if vma > 0 and volumes[i] > 2.0 * vma:
            vol_spike_days += 1

    adx_t1 = adx_arr[t - 1]
    adx_t15 = adx_arr[t - 15]
    adx_change = adx_t1 - adx_t15

    above_sma50_days = sum(
        1 for i in range(w15_start, w15_end + 1)
        if sma50[i] > 0 and closes[i] > sma50[i]
    )
    pct_above_sma50 = above_sma50_days / 15.0 * 100.0

    cons_high = max(highs[w10_start : w15_end + 1])
    cons_low = min(lows[w10_start : w15_end + 1])
    consolidation_pct = (cons_high - cons_low) / close_t1 * 100.0 if close_t1 > 0 else 0.0

    prior_spike = False
    prior_spike_max = 0.0
    for i in range(w15_start, spike_end + 1):
        if closes[i - 1] <= 0:
            continue
        chg = _pct(closes[i], closes[i - 1])
        prior_spike_max = max(prior_spike_max, chg)
        if chg > 8.0:
            prior_spike = True

    high_20_start = max(0, t - 20)
    high_20 = max(highs[high_20_start : t])
    dist_from_20d_high = _pct(close_t1, high_20)

    return {
        "cum_ret_t15_t1": round(cum_ret_15, 4),
        "cum_ret_t10_t1": round(cum_ret_10, 4),
        "cum_ret_t5_t1": round(cum_ret_5, 4),
        "max_close_vs_t_pct": round(max_close_vs_t, 4),
        "min_close_vs_t_pct": round(min_close_vs_t, 4),
        "vol_trend_last5_vs_prior10": round(vol_trend, 4) if vol_trend is not None else None,
        "vol_spike_days_gt_2x": vol_spike_days,
        "adx_t1": round(adx_t1, 2),
        "adx_t15": round(adx_t15, 2),
        "adx_change": round(adx_change, 2),
        "adx_rising": adx_change > 0,
        "rsi_t1": round(rsi[t - 1], 2),
        "pct_days_above_sma50": round(pct_above_sma50, 1),
        "consolidation_range_pct": round(consolidation_pct, 2),
        "prior_spike_gt_8pct": prior_spike,
        "prior_spike_max_pct": round(prior_spike_max, 2),
        "dist_from_20d_high_pct": round(dist_from_20d_high, 2),
    }


def _cohort_stats(rows: list[dict[str, Any]], numeric_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(rows)}
    for key in numeric_keys:
        vals = [r["features"][key] for r in rows if r["features"].get(key) is not None]
        if not vals:
            out[key] = None
            continue
        out[key] = {
            "mean": round(statistics.mean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        }
    bool_keys = ["adx_rising", "prior_spike_gt_8pct"]
    for key in bool_keys:
        vals = [r["features"][key] for r in rows if key in r["features"]]
        out[key] = round(sum(1 for v in vals if v) / len(vals) * 100, 1) if vals else None
    return out


def _delta(w: dict[str, Any], f: dict[str, Any], key: str) -> str:
    wv = (w.get(key) or {}).get("mean") if isinstance(w.get(key), dict) else w.get(key)
    fv = (f.get(key) or {}).get("mean") if isinstance(f.get(key), dict) else f.get(key)
    if wv is None or fv is None:
        return "n/a"
    if isinstance(wv, (int, float)) and isinstance(fv, (int, float)):
        return f"{wv - fv:+.2f}"
    return "n/a"


def _build_filter_recommendations(
    winners: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive concrete pre-filters from winner vs failure separation."""
    recs: list[dict[str, Any]] = []

    def fail_rate(predicate) -> tuple[float, int, int]:
        f_hit = sum(1 for r in failures if predicate(r))
        w_miss = sum(1 for r in winners if not predicate(r))
        total_f = len(failures)
        total_w = len(winners)
        blocked_fail_pct = f_hit / total_f * 100 if total_f else 0
        kept_win_pct = (total_w - w_miss) / total_w * 100 if total_w else 0
        return blocked_fail_pct, f_hit, w_miss

    # Prior run-up filters
    for thresh in (15, 20, 25, 30):
        blocked, f_hit, w_miss = fail_rate(
            lambda r, t=thresh: r["features"]["cum_ret_t10_t1"] <= t
        )
        recs.append({
            "rule": f"skip_if_cum_ret_t10_t1_gt_{thresh}pct",
            "description": f"Skip if already +{thresh}% from T-10 to T-1",
            "failures_blocked_pct": round(blocked, 1),
            "failures_blocked": f_hit,
            "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
            "winners_lost": w_miss,
        })

    # ADX rising
    blocked, f_hit, w_miss = fail_rate(lambda r: r["features"]["adx_rising"])
    recs.append({
        "rule": "require_adx_rising_t15_to_t1",
        "description": "Require ADX(T-1) > ADX(T-15) — trend strengthening into signal",
        "failures_blocked_pct": round(blocked, 1),
        "failures_blocked": f_hit,
        "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
        "winners_lost": w_miss,
    })

    # Consolidation ceiling
    for thresh in (12, 15, 18, 20):
        blocked, f_hit, w_miss = fail_rate(
            lambda r, t=thresh: r["features"]["consolidation_range_pct"] <= t
        )
        recs.append({
            "rule": f"require_consolidation_range_lte_{thresh}pct",
            "description": f"Require T-10..T-1 range/close ≤ {thresh}% (tight base)",
            "failures_blocked_pct": round(blocked, 1),
            "failures_blocked": f_hit,
            "winners_kept_pct": round(100 - len(winners) and (len(winners) - w_miss) / len(winners) * 100, 1),
            "winners_lost": w_miss,
        })

    # Prior spike
    blocked, f_hit, w_miss = fail_rate(lambda r: not r["features"]["prior_spike_gt_8pct"])
    recs.append({
        "rule": "skip_if_prior_spike_gt_8pct_in_t15_t5",
        "description": "Skip if any day T-15..T-5 had >+8% single-session move",
        "failures_blocked_pct": round(blocked, 1),
        "failures_blocked": f_hit,
        "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
        "winners_lost": w_miss,
    })

    # Distance from 20d high (not extended)
    for thresh in (-3, -5, -8):
        blocked, f_hit, w_miss = fail_rate(
            lambda r, t=thresh: r["features"]["dist_from_20d_high_pct"] >= t
        )
        recs.append({
            "rule": f"require_within_{abs(thresh)}pct_of_20d_high",
            "description": f"Require T-1 close within {abs(thresh)}% of 20-day high (not extended)",
            "failures_blocked_pct": round(blocked, 1),
            "failures_blocked": f_hit,
            "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
            "winners_lost": w_miss,
        })

    # SMA50 alignment
    blocked, f_hit, w_miss = fail_rate(lambda r: r["features"]["pct_days_above_sma50"] >= 80)
    recs.append({
        "rule": "require_80pct_days_above_sma50",
        "description": "Require ≥80% of T-15..T-1 closes above SMA50",
        "failures_blocked_pct": round(blocked, 1),
        "failures_blocked": f_hit,
        "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
        "winners_lost": w_miss,
    })

    # Vol spike days
    blocked, f_hit, w_miss = fail_rate(lambda r: r["features"]["vol_spike_days_gt_2x"] <= 2)
    recs.append({
        "rule": "max_2_vol_spike_days_in_prior_15",
        "description": "Skip if >2 days in T-15..T-1 had volume >2× 20d avg",
        "failures_blocked_pct": round(blocked, 1),
        "failures_blocked": f_hit,
        "winners_kept_pct": round(100 - w_miss / len(winners) * 100, 1) if winners else 0,
        "winners_lost": w_miss,
    })

    recs.sort(key=lambda x: (-x["failures_blocked_pct"], -x["winners_kept_pct"]))
    return recs


def _fmt(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%" if signed else f"{v:.2f}"


def build_markdown(report: dict[str, Any]) -> str:
    w = report["cohort_comparison"]["winners"]
    f = report["cohort_comparison"]["failures"]
    signals = report["signals"]
    recs = report["recommendations"]

    lines = [
        "# T-15 Pre-Signal Pattern Report — v2 PASS Cohort",
        "",
        f"**Generated:** {report['generated_at']}  ",
        f"**Branch:** `{report['branch']}`  ",
        f"**Signals analyzed:** {report['signal_count']}  ",
        f"**Winners:** {report['winner_count']} | **Failures:** {report['failure_count']} | **Mixed:** {report['mixed_count']}",
        "",
        "---",
        "",
        "## Outcome Classification (T+20 report)",
        "",
        "**Winners:** never closed below entry OR T+15 close ≥ +8% vs entry  ",
        "**Failures:** max drawdown < −10% OR T+15 close < 0% OR underwater >50% of T+20 sessions",
        "",
        "### Cohort labels",
        "",
        "| Symbol | Signal date | Label | T+15 | Max DD | Winner reason | Failure reason |",
        "| :--- | :--- | :--- | ---: | ---: | :--- | :--- |",
    ]

    for s in signals:
        oc = s["outcome"]
        lines.append(
            f"| {s['symbol']} | {s['signal_date']} | **{oc['label']}** | "
            f"{_fmt(oc['t15_vs_entry_pct'])} | {_fmt(oc['max_drawdown_pct'])} | "
            f"{'; '.join(oc['winner_reasons']) or '—'} | "
            f"{'; '.join(oc['failure_reasons']) or '—'} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## Cohort Comparison — T-15..T-1 Features",
        "",
        "| Feature | Winners (mean) | Failures (mean) | Δ (W−F) |",
        "| :--- | ---: | ---: | ---: |",
    ])

    feature_keys = [
        ("cum_ret_t15_t1", "Cum return T-15→T-1"),
        ("cum_ret_t10_t1", "Cum return T-10→T-1"),
        ("cum_ret_t5_t1", "Cum return T-5→T-1"),
        ("max_close_vs_t_pct", "Max close (window) vs T"),
        ("min_close_vs_t_pct", "Min close (window) vs T"),
        ("vol_trend_last5_vs_prior10", "Vol trend (last5/prior10)"),
        ("vol_spike_days_gt_2x", "Days vol >2× (prior 15)"),
        ("adx_change", "ADX change T-15→T-1"),
        ("rsi_t1", "RSI at T-1"),
        ("pct_days_above_sma50", "% days above SMA50"),
        ("consolidation_range_pct", "Consolidation range T-10..T-1"),
        ("prior_spike_max_pct", "Max single-day spike T-15..T-5"),
        ("dist_from_20d_high_pct", "Dist from 20d high at T-1"),
    ]

    for key, label in feature_keys:
        wm = (w.get(key) or {}).get("mean") if isinstance(w.get(key), dict) else w.get(key)
        fm = (f.get(key) or {}).get("mean") if isinstance(f.get(key), dict) else f.get(key)
        delta = _delta(w, f, key)
        wm_s = f"{wm:.2f}" if isinstance(wm, (int, float)) else "n/a"
        fm_s = f"{fm:.2f}" if isinstance(fm, (int, float)) else "n/a"
        lines.append(f"| {label} | {wm_s} | {fm_s} | {delta} |")

    lines.extend([
        f"| ADX rising (T-15→T-1) | {w.get('adx_rising', 'n/a')}% | {f.get('adx_rising', 'n/a')}% | — |",
        f"| Prior spike >8% | {w.get('prior_spike_gt_8pct', 'n/a')}% | {f.get('prior_spike_gt_8pct', 'n/a')}% | — |",
        "",
        "---",
        "",
        "## Distinguishing Patterns",
        "",
    ])

    patterns = report.get("distinguishing_patterns") or []
    if patterns:
        for p in patterns:
            lines.append(f"- **{p['feature']}:** {p['summary']}")
    else:
        lines.append("- Insufficient separation for strong patterns.")

    lines.extend([
        "",
        "---",
        "",
        "## Recommended Pre-Filters",
        "",
        "Ranked by failure-block rate (higher = more failures removed). "
        "Review winners_lost before adopting.",
        "",
        "| Rule | Failures blocked | Winners kept | Winners lost |",
        "| :--- | ---: | ---: | ---: |",
    ])

    for r in recs[:12]:
        lines.append(
            f"| `{r['rule']}` | {r['failures_blocked']}/{report['failure_count']} "
            f"({r['failures_blocked_pct']}%) | {r['winners_kept_pct']}% | {r['winners_lost']} |"
        )

    lines.extend([
        "",
        "### Top rules (plain language)",
        "",
    ])
    for i, r in enumerate(recs[:5], 1):
        lines.append(
            f"{i}. **{r['description']}** — blocks {r['failures_blocked_pct']}% of failures, "
            f"keeps {r['winners_kept_pct']}% of winners ({r['winners_lost']} winner(s) lost)."
        )

    lines.extend([
        "",
        "---",
        "",
        "## Per-Signal Feature Detail",
        "",
    ])

    for s in signals:
        feat = s["features"]
        oc = s["outcome"]
        lines.extend([
            f"### {s['symbol']} — {s['signal_date']} ({oc['label']})",
            "",
            f"| Feature | Value |",
            f"| :--- | ---: |",
            f"| Cum ret T-15→T-1 | {_fmt(feat['cum_ret_t15_t1'])} |",
            f"| Cum ret T-10→T-1 | {_fmt(feat['cum_ret_t10_t1'])} |",
            f"| Vol trend (5/10) | {feat['vol_trend_last5_vs_prior10'] or 'n/a'} |",
            f"| Vol spike days | {feat['vol_spike_days_gt_2x']} |",
            f"| ADX T-1 / change | {feat['adx_t1']} / {feat['adx_change']:+.2f} ({'rising' if feat['adx_rising'] else 'falling'}) |",
            f"| RSI T-1 | {feat['rsi_t1']} |",
            f"| % above SMA50 | {feat['pct_days_above_sma50']:.1f}% |",
            f"| Consolidation range | {feat['consolidation_range_pct']:.2f}% |",
            f"| Prior spike >8% | {'Yes' if feat['prior_spike_gt_8pct'] else 'No'} (max {feat['prior_spike_max_pct']:+.2f}%) |",
            f"| Dist from 20d high | {_fmt(feat['dist_from_20d_high_pct'])} |",
            "",
        ])

    lines.append(
        "*Educational backtest only. Pre-signal window = T-15..T-1 trading sessions before signal close. "
        "Bars from `data/cache/breakout_yahoo/backtest/`.*"
    )
    return "\n".join(lines)


def _find_distinguishing_patterns(
    winners: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if not winners or not failures:
        return []

    patterns: list[dict[str, str]] = []
    checks = [
        ("cum_ret_t10_t1", "Failures ran up more in prior 10 sessions", lambda w, f: f > w + 5),
        ("adx_change", "Winners show rising ADX into signal", lambda w, f: w > f + 2),
        ("consolidation_range_pct", "Failures had wider pre-signal bases", lambda w, f: f > w + 3),
        ("prior_spike_max_pct", "Failures had larger prior single-day spikes", lambda w, f: f > w + 3),
        ("dist_from_20d_high_pct", "Failures were more extended vs 20d high", lambda w, f: f > w + 2),
        ("vol_spike_days_gt_2x", "Failures had more high-volume days pre-signal", lambda w, f: f > w + 0.5),
        ("pct_days_above_sma50", "Winners spent more time above SMA50", lambda w, f: w > f + 10),
    ]

    for key, desc, test in checks:
        w_vals = [r["features"][key] for r in winners if r["features"].get(key) is not None]
        f_vals = [r["features"][key] for r in failures if r["features"].get(key) is not None]
        if not w_vals or not f_vals:
            continue
        wm, fm = statistics.mean(w_vals), statistics.mean(f_vals)
        if test(wm, fm):
            patterns.append({
                "feature": key,
                "summary": f"{desc}: winners avg {wm:.2f} vs failures {fm:.2f} (Δ {wm - fm:+.2f})",
            })

    w_adx_rise = sum(1 for r in winners if r["features"]["adx_rising"]) / len(winners) * 100
    f_adx_rise = sum(1 for r in failures if r["features"]["adx_rising"]) / len(failures) * 100
    if w_adx_rise > f_adx_rise + 15:
        patterns.append({
            "feature": "adx_rising",
            "summary": f"Winners more often have rising ADX: {w_adx_rise:.0f}% vs {f_adx_rise:.0f}%",
        })

    w_spike = sum(1 for r in winners if r["features"]["prior_spike_gt_8pct"]) / len(winners) * 100
    f_spike = sum(1 for r in failures if r["features"]["prior_spike_gt_8pct"]) / len(failures) * 100
    if f_spike > w_spike + 15:
        patterns.append({
            "feature": "prior_spike_gt_8pct",
            "summary": f"Failures more often had prior >8% spike: {f_spike:.0f}% vs {w_spike:.0f}%",
        })

    return patterns


def main() -> int:
    if not T20_JSON.is_file():
        print(f"Missing {T20_JSON}", file=sys.stderr)
        return 1

    t20 = json.loads(T20_JSON.read_text(encoding="utf-8"))
    universe = json.loads((OUT_DIR / "universe_40.json").read_text(encoding="utf-8"))
    ticker_map = {e["symbol"]: e["yahoo_ticker"] for e in universe["stocks"]}

    branch = "main"
    try:
        import subprocess
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        if r.stdout.strip():
            branch = r.stdout.strip()
    except OSError:
        pass

    numeric_keys = [
        "cum_ret_t15_t1", "cum_ret_t10_t1", "cum_ret_t5_t1",
        "max_close_vs_t_pct", "min_close_vs_t_pct",
        "vol_trend_last5_vs_prior10", "vol_spike_days_gt_2x",
        "adx_change", "rsi_t1", "pct_days_above_sma50",
        "consolidation_range_pct", "prior_spike_max_pct", "dist_from_20d_high_pct",
    ]

    analyzed: list[dict[str, Any]] = []
    skipped: list[str] = []

    for sig in t20["signals"]:
        sym = sig["symbol"]
        ticker = ticker_map.get(sym, f"{sym}.NS")
        df = load_backtest_bars(ticker)
        if not df:
            skipped.append(f"{sym}: no cache")
            continue

        bar_idx = sig["bar_idx"]
        # Verify bar date alignment
        features = compute_pre_signal_features(df, bar_idx)
        if not features:
            skipped.append(f"{sym}: insufficient bars at idx {bar_idx}")
            continue

        outcome = classify_outcome(sig)
        analyzed.append({
            "symbol": sym,
            "signal_date": sig["signal_date"],
            "bar_idx": bar_idx,
            "entry": sig["entry"],
            "pass_paths": sig.get("pass_paths"),
            "outcome": outcome,
            "features": features,
        })

    winners = [r for r in analyzed if r["outcome"]["label"] == "winner"]
    failures = [r for r in analyzed if r["outcome"]["label"] == "failure"]
    mixed = [r for r in analyzed if r["outcome"]["label"] == "mixed"]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "branch": branch,
        "signal_count": len(analyzed),
        "winner_count": len(winners),
        "failure_count": len(failures),
        "mixed_count": len(mixed),
        "skipped": skipped,
        "classification_rules": {
            "winner": "never_below_entry OR t15_vs_entry >= 8%",
            "failure": "max_drawdown < -10% OR t15_vs_entry < 0% OR underwater >50% of T+20",
        },
        "cohort_comparison": {
            "winners": _cohort_stats(winners, numeric_keys),
            "failures": _cohort_stats(failures, numeric_keys),
            "mixed": _cohort_stats(mixed, numeric_keys) if mixed else None,
        },
        "distinguishing_patterns": _find_distinguishing_patterns(winners, failures),
        "recommendations": _build_filter_recommendations(winners, failures),
        "signals": analyzed,
    }

    md_path = OUT_DIR / "t15_pre_signal_pattern_report.md"
    json_path = OUT_DIR / "t15_pre_signal_pattern_report.json"

    md_path.write_text(build_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Analyzed: {len(analyzed)}, winners: {len(winners)}, failures: {len(failures)}, mixed: {len(mixed)}")
    if skipped:
        print(f"Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
