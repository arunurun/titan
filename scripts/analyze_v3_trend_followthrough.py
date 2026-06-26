#!/usr/bin/env python3
"""v3 PASS signal trend follow-through analysis (100-stock 6m cohort)."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from breakout_backtest import (  # noqa: E402
    load_cached_universe_history,
    replay_stock,
)
from breakout_scanner import bar_dates_from_df  # noqa: E402

# Reuse T-15 feature helpers from sibling script
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_t15_pre_signal_patterns import (  # noqa: E402
    compute_pre_signal_features,
)

OUT_DIR = ROOT / "output" / "breakoutcheck"
V5_FIX_CHECKLIST = OUT_DIR / "v5_fix_checklist.md"
V6_FIX_CHECKLIST = OUT_DIR / "v6_fix_checklist.md"
V7_FIX_CHECKLIST = OUT_DIR / "v7_fix_checklist.md"
DEFAULT_UNIVERSE = OUT_DIR / "universe_40.json"
FORWARD_DAYS = 20
CLASSIFY_HORIZON = 15
V3_BASELINE = {
    "total_pass_signals": 48,
    "follow_through_count": 15,
    "follow_through_rate": 31.25,
    "no_follow_through_count": 22,
    "no_follow_through_rate": 45.83,
    "mixed_count": 11,
    "label": "v3 pre-fix (100-stock 6m cohort)",
}
V4_BASELINE = {
    "total_pass_signals": 20,
    "follow_through_count": 8,
    "follow_through_rate": 40.0,
    "no_follow_through_count": 7,
    "no_follow_through_rate": 35.0,
    "mixed_count": 5,
    "label": "v4 post-fix (100-stock 6m cohort)",
}
V5_BASELINE = {
    "total_pass_signals": 18,
    "total_watch_signals": 1,
    "follow_through_count": 7,
    "follow_through_rate": 38.89,
    "no_follow_through_count": 6,
    "no_follow_through_rate": 33.33,
    "mixed_count": 5,
    "label": "v5 post-fix (100-stock 6m cohort)",
}
V6_BASELINE = {
    "total_pass_signals": 14,
    "total_watch_signals": 3,
    "follow_through_count": 8,
    "follow_through_rate": 57.14,
    "no_follow_through_count": 2,
    "no_follow_through_rate": 14.29,
    "mixed_count": 4,
    "label": "v6 post-fix (100-stock 6m cohort)",
}


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def build_forward_path(
    df: dict[str, Any],
    bar_idx: int,
    entry: float,
    *,
    max_days: int = FORWARD_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Daily close path T+1..T+max_days vs entry."""
    closes = df["close"]
    n = len(closes)
    daily: list[dict[str, Any]] = []
    min_close = entry
    max_close = entry
    ever_below = False
    below_days = 0
    observed = 0

    for day in range(1, max_days + 1):
        j = bar_idx + day
        if j >= n:
            daily.append({"day": day, "insufficient_bars": True})
            continue
        c = closes[j]
        vs = _pct(c, entry)
        below = c < entry
        if below:
            ever_below = True
            below_days += 1
        min_close = min(min_close, c)
        max_close = max(max_close, c)
        observed += 1
        daily.append({
            "day": day,
            "close": round(c, 2),
            "vs_entry_pct": round(vs, 4),
            "below_entry": below,
            "insufficient_bars": False,
        })

    max_dd = _pct(min_close, entry) if observed else None
    final_t20 = None
    if bar_idx + max_days < n:
        final_t20 = _pct(closes[bar_idx + max_days], entry)

    dip_summary = {
        "max_drawdown_vs_entry_pct": round(max_dd, 4) if max_dd is not None else None,
        "ever_below_entry": ever_below,
        "below_entry_days": below_days,
        "observed_days": observed,
        "final_t20_vs_entry_pct": round(final_t20, 4) if final_t20 is not None else None,
        "max_close_vs_entry_pct": round(_pct(max_close, entry), 4) if observed else None,
    }
    return daily, dip_summary


def classify_follow_through(
    *,
    daily_path: list[dict[str, Any]],
    dip_summary: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Explicit follow-through vs fail/choppy criteria."""
    t15_row = next((r for r in daily_path if r.get("day") == CLASSIFY_HORIZON and not r.get("insufficient_bars")), None)
    t15_pct = t15_row.get("vs_entry_pct") if t15_row else None

    eff = outcome.get("efficacy") or {}
    mfe = outcome.get("mfe_pct")
    mae = outcome.get("mae_pct")
    ever_below = bool(dip_summary.get("ever_below_entry"))
    max_dd = dip_summary.get("max_drawdown_vs_entry_pct")
    below_days = dip_summary.get("below_entry_days", 0)
    observed = dip_summary.get("observed_days", 0)
    underwater_pct = (below_days / observed * 100.0) if observed else 0.0

    t15_horizon = (outcome.get("horizons") or {}).get("t15") or {}
    target_win = t15_horizon.get("win") is True
    stopped = outcome.get("first_exit") == "loss" and (outcome.get("first_exit_bar") or 99) <= 10

    follow_reasons: list[str] = []
    fail_reasons: list[str] = []

    if t15_pct is not None and t15_pct >= 5.0:
        follow_reasons.append(f"t15_ge_5pct ({t15_pct:+.2f}%)")
    if eff.get("mfe_hit_8") and t15_pct is not None and t15_pct >= 0:
        follow_reasons.append(f"mfe_ge_8_and_t15_nonneg (MFE {mfe:+.2f}%)")
    if target_win:
        follow_reasons.append("target_hit_within_t15")
    if not ever_below and t15_pct is not None and t15_pct >= 3.0:
        follow_reasons.append(f"never_below_entry_t15_ge_3 ({t15_pct:+.2f}%)")
    if t15_pct is not None and t15_pct >= 8.0:
        follow_reasons.append(f"t15_ge_8pct ({t15_pct:+.2f}%)")

    if t15_pct is not None and t15_pct < 0:
        fail_reasons.append(f"t15_lt_0 ({t15_pct:+.2f}%)")
    if mae is not None and mae < -8.0 and (t15_pct is None or t15_pct < 3.0):
        fail_reasons.append(f"mae_lt_-8_weak_t15 (MAE {mae:+.2f}%)")
    if stopped:
        fail_reasons.append(f"stopped_out_within_t10 (bar {outcome.get('first_exit_bar')})")
    if max_dd is not None and max_dd < -10.0:
        fail_reasons.append(f"max_dd_lt_-10 ({max_dd:+.2f}%)")
    if observed and below_days > observed / 2:
        fail_reasons.append(f"underwater_most_t{CLASSIFY_HORIZON} ({below_days}/{observed} days)")

    is_follow = bool(follow_reasons)
    is_fail = bool(fail_reasons)

    if is_follow and not is_fail:
        label = "follow_through"
    elif is_fail and not is_follow:
        label = "no_follow_through"
    elif is_follow and is_fail:
        # Strong T+15 / target hit overrides dip noise
        if (t15_pct is not None and t15_pct >= 5.0) or target_win:
            label = "follow_through"
        else:
            label = "no_follow_through"
    else:
        label = "mixed"

    return {
        "label": label,
        "is_follow_through": label == "follow_through",
        "is_no_follow_through": label == "no_follow_through",
        "follow_reasons": follow_reasons,
        "fail_reasons": fail_reasons,
        "t15_vs_entry_pct": t15_pct,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "max_drawdown_pct": max_dd,
        "ever_below_entry": ever_below,
        "underwater_days": below_days,
        "underwater_pct": round(underwater_pct, 1),
    }


def _prior_pass_within_window(
    signals_by_symbol: dict[str, list[dict[str, Any]]],
    sym: str,
    bar_idx: int,
    window: int = 20,
) -> bool:
    for prior in signals_by_symbol.get(sym, []):
        if prior["bar_idx"] < bar_idx <= prior["bar_idx"] + window:
            return True
    return False


def categorize_root_causes(
    sig: dict[str, Any],
    *,
    features: dict[str, Any],
    follow: dict[str, Any],
    signals_by_symbol: dict[str, list[dict[str, Any]]],
    df: dict[str, Any],
) -> list[str]:
    """Assign failure root-cause tags (multi-label)."""
    if follow.get("is_follow_through"):
        return []

    causes: list[str] = []
    metrics = sig.get("metrics") or {}
    pass_paths = metrics.get("pass_paths") or []
    pct = metrics.get("pct_change") or 0.0
    adx = metrics.get("adx_val") or 0.0
    sym = sig["symbol"]
    bar_idx = sig["bar_idx"]

    if "power_gap" in pass_paths:
        causes.append("power_gap_fade_chase")
    if pass_paths == ["adx_soft"] or (len(pass_paths) == 1 and pass_paths[0] == "adx_soft"):
        causes.append("adx_soft_only_no_confirmation")
    elif "adx_soft" in pass_paths and "adx_rising" in features and not features.get("adx_rising"):
        causes.append("adx_soft_without_trajectory")

    if features.get("cum_ret_t10_t1", 0) > 25 and features.get("prior_spike_gt_8pct"):
        causes.append("rumour_sector_sympathy_extended_run")
    elif features.get("cum_ret_t10_t1", 0) > 20:
        causes.append("extended_pre_trend_chase")

    if _prior_pass_within_window(signals_by_symbol, sym, bar_idx):
        causes.append("repeat_signal_on_extended_name")
    if features.get("dist_from_20d_high_pct", -999) > -2:
        causes.append("repeat_signal_on_extended_name")

    pre_val = metrics.get("pre_validation") or {}
    cum_pre = pre_val.get("cum_return_t10_t1")
    if cum_pre is not None and cum_pre > 25:
        causes.append("pre_filter_gap_cum_return")
    if features.get("vol_spike_days_gt_2x", 0) > 2:
        causes.append("pre_filter_gap_vol_spike")
    if features.get("prior_spike_gt_8pct") and cum_pre is not None and cum_pre > 15:
        causes.append("pre_filter_gap_prior_spike_runup")

    # Entry-at-close: large signal-day move, next session opens weak
    closes = df["close"]
    opens = df["open"]
    entry = metrics.get("latest_price") or closes[bar_idx]
    if bar_idx + 1 < len(opens) and pct >= 10.0:
        if opens[bar_idx + 1] < entry * 0.98:
            causes.append("entry_at_close_timing_gap_down")

    if not features.get("adx_rising"):
        causes.append("missing_adx_trajectory")
    if features.get("vol_trend_last5_vs_prior10") is not None and features["vol_trend_last5_vs_prior10"] < 0.8:
        causes.append("weak_volume_trajectory")

    if "rsi_hot" in pass_paths and follow.get("t15_vs_entry_pct") is not None and follow["t15_vs_entry_pct"] < 0:
        causes.append("rsi_hot_overextension")

    if not causes:
        causes.append("unclassified_choppy")

    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for c in causes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _suggest_fixes(
    root_cause_counts: Counter,
    failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map root-cause patterns to actionable code fixes."""
    fixes: list[dict[str, Any]] = []

    if root_cause_counts.get("power_gap_fade_chase", 0) >= 2:
        fixes.append({
            "priority": 1,
            "area": "pass_paths / power_gap",
            "fix": "Require ADX rising OR cum_ret_t10_t1 ≤ 15% when power_gap path is used; "
                   "otherwise downgrade to WATCH-only with tighter sizing.",
            "signals_affected": root_cause_counts["power_gap_fade_chase"],
        })

    if root_cause_counts.get("adx_soft_only_no_confirmation", 0) + root_cause_counts.get("adx_soft_without_trajectory", 0) >= 3:
        n = root_cause_counts.get("adx_soft_only_no_confirmation", 0) + root_cause_counts.get("adx_soft_without_trajectory", 0)
        fixes.append({
            "priority": 2,
            "area": "adx_soft path",
            "fix": "Tighten adx_soft: require ADX(T-1) > ADX(T-5) and vol_mult ≥ tier+0.5x; "
                   "block adx_soft when cum_ret_t10_t1 > 20%.",
            "signals_affected": n,
        })

    if root_cause_counts.get("pre_filter_gap_cum_return", 0) + root_cause_counts.get("extended_pre_trend_chase", 0) >= 2:
        n = root_cause_counts.get("pre_filter_gap_cum_return", 0) + root_cause_counts.get("extended_pre_trend_chase", 0)
        fixes.append({
            "priority": 3,
            "area": "pre_signal_validation",
            "fix": "Lower PRE_SIGNAL_CUM_RETURN_MAX from 30% to 20–25% for micro-cap; "
                   "add T-5..T-1 sub-window cap (+12%) to catch late-stage chases.",
            "signals_affected": n,
        })

    if root_cause_counts.get("repeat_signal_on_extended_name", 0) >= 2:
        fixes.append({
            "priority": 4,
            "area": "signal dedup",
            "fix": "Add cooldown: no new PASS within 20 sessions of prior PASS on same symbol unless "
                   "consolidation_range_pct ≤ 12% and dist_from_20d_high ≥ -3%.",
            "signals_affected": root_cause_counts["repeat_signal_on_extended_name"],
        })

    if root_cause_counts.get("entry_at_close_timing_gap_down", 0) >= 2:
        fixes.append({
            "priority": 5,
            "area": "entry timing",
            "fix": "Flag entry-at-close on >10% signal days; require next-day open ≥ entry×0.99 "
                   "or use entry band (entry_low..entry_high) for forward validation.",
            "signals_affected": root_cause_counts["entry_at_close_timing_gap_down"],
        })

    if root_cause_counts.get("missing_adx_trajectory", 0) >= 3:
        fixes.append({
            "priority": 6,
            "area": "indicator trajectory",
            "fix": "Add ADX slope check (T-1 vs T-10) as hard gate when pass_paths includes adx_soft or rsi_hot.",
            "signals_affected": root_cause_counts["missing_adx_trajectory"],
        })

    fixes.sort(key=lambda x: (-x["signals_affected"], x["priority"]))
    return fixes[:5]


def _v5_implemented_filters() -> list[dict[str, str]]:
    """Filters shipped in v5 — mirrors output/breakoutcheck/v5_fix_checklist.md."""
    return [
        {
            "checklist_ref": "Fix 1",
            "description": (
                "ADX trajectory gate (T-1 vs T-10) for `adx_soft` / `rsi_hot`; "
                "fail reason `pre_filter_adx_trajectory`"
            ),
        },
        {
            "checklist_ref": "Fix 1b",
            "description": (
                "ADX short slope for `adx_soft`: ADX(T-1) > ADX(T-5) "
                "(PRE_SIGNAL_ADX_SOFT_SHORT_LOOKBACK = 5)"
            ),
        },
        {
            "checklist_ref": "Fix 2a",
            "description": (
                "`adx_soft` elevated volume: vol_mult ≥ tier + 0.5 (ADX_SOFT_VOL_BONUS)"
            ),
        },
        {
            "checklist_ref": "Fix 2b",
            "description": (
                "`adx_soft` chase block when cum_ret_t10_t1 > 20%; "
                "fail reason `pre_filter_adx_soft_chase`"
            ),
        },
        {
            "checklist_ref": "Fix 3",
            "description": (
                "`power_gap` confirmation: PASS when ADX rising OR cum_ret_t10_t1 ≤ 15%; "
                "else WATCH with 1% sizing cap"
            ),
        },
        {
            "checklist_ref": "Cooldown",
            "description": (
                "Repeat PASS blocked within 20 sessions unless consolidation exempt; "
                "fail reason `pre_filter_signal_cooldown`"
            ),
        },
        {
            "checklist_ref": "Pre-signal validation",
            "description": (
                "Cumulative return T-10→T-1 > 30% and volume spike days > 2 gates "
                "(baseline, unchanged in v5)"
            ),
        },
    ]


def _v7_implemented_filters() -> list[dict[str, str]]:
    """Filters shipped in v7 — mirrors output/breakoutcheck/v7_fix_checklist.md."""
    return [
        {
            "checklist_ref": "V7-A",
            "description": (
                "Liquidity hard gate: small-cap median turnover ≥ ₹2cr, micro-cap ≥ ₹3cr "
                "(bhav turnover or median notional proxy); fail `pre_filter_liquidity`"
            ),
        },
        {
            "checklist_ref": "V7-B",
            "description": (
                "Volume persistence: micro PASS requires score ≥ 2, small PASS requires ≥ 1; "
                "else WATCH (`v7_low_volume_persistence`)"
            ),
        },
        {
            "checklist_ref": "V7-C",
            "description": (
                "Breakout stage 3 (parabolic, stretch > 4 ATR) → WATCH only "
                "(`v7_breakout_stage_3`); stage 1 preferred via ranking"
            ),
        },
        {
            "checklist_ref": "V7-D",
            "description": (
                "Base quality score in candidate metrics; composite rank weights: "
                "Breakout 25%, Sector 20%, Base 15%, Persistence 15%, "
                "Acceleration 10%, RS 10%, Risk 5%"
            ),
        },
    ]


def _v6_implemented_filters() -> list[dict[str, str]]:
    """Filters shipped in v6 — mirrors output/breakoutcheck/v6_fix_checklist.md."""
    return [
        {
            "checklist_ref": "V6-A",
            "description": (
                "Solo `adx_soft` only (`pass_paths == ['adx_soft']`) → WATCH with 1% sizing; "
                "`passed=False` but emitted as WATCH candidate"
            ),
        },
        {
            "checklist_ref": "V6-B",
            "description": (
                "Standard path ADX trajectory: require ADX(T-1) > ADX(T-10) unless "
                f"vol_mult >= {7.0} (STANDARD_ADX_TRAJECTORY_VOL_EXCEPTION); "
                "fail reason `pre_filter_standard_adx_trajectory`"
            ),
        },
        {
            "checklist_ref": "V6-C",
            "description": (
                "power_gap vol recovery: unconfirmed gap may PASS when "
                f"vol_mult >= {5.5} (POWER_GAP_VOL_RECOVERY_THRESHOLD)"
            ),
        },
    ]


def build_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    summary = report["summary"]
    baseline = report.get("baseline_comparison") or {}
    title = meta.get("report_title") or "v3 Breakout Trend Follow-Through Analysis (100-Stock Cohort)"
    lines = [
        f"# {title}",
        "",
        f"**Generated:** {meta['generated_at']}  ",
        f"**Commit:** `{meta['commit']}`  ",
        f"**Universe:** {meta['small_cap_count']} small-cap + {meta['micro_cap_count']} micro-cap  ",
        f"**Date range:** {meta['date_range']['first']} → {meta['date_range']['last']}  ",
        f"**Stocks replayed:** {meta['stocks_replayed']} / {meta['universe_total']}  ",
        "",
    ]
    if baseline:
        lines.extend([
            "---",
            "",
            "## Before / After (v3 fixes)",
            "",
            "| Metric | Baseline | After fixes | Δ |",
            "| :--- | ---: | ---: | ---: |",
            f"| PASS signals | {baseline['baseline']['total_pass_signals']} | "
            f"{baseline['after']['total_pass_signals']} | "
            f"{baseline['delta']['total_pass_signals']:+d} |",
            f"| Follow-through | {baseline['baseline']['follow_through_count']} "
            f"({baseline['baseline']['follow_through_rate']}%) | "
            f"{baseline['after']['follow_through_count']} "
            f"({baseline['after']['follow_through_rate']}%) | "
            f"{baseline['delta']['follow_through_count']:+d} |",
            f"| Follow-through rate | {baseline['baseline']['follow_through_rate']}% | "
            f"{baseline['after']['follow_through_rate']}% | "
            f"{baseline['delta']['follow_through_rate']:+.2f}pp |",
            f"| No follow-through | {baseline['baseline']['no_follow_through_count']} "
            f"({baseline['baseline']['no_follow_through_rate']}%) | "
            f"{baseline['after']['no_follow_through_count']} "
            f"({baseline['after']['no_follow_through_rate']}%) | "
            f"{baseline['delta']['no_follow_through_count']:+d} |",
        ])
        if baseline.get("after", {}).get("total_watch_signals") is not None:
            lines.append(
                f"| WATCH signals | — | {baseline['after']['total_watch_signals']} | — |"
            )
        lines.extend([
            "",
            f"*Baseline: {baseline['baseline_label']}*",
            "",
        ])
    multi = report.get("multi_baseline_comparison") or {}
    if multi:
        version_cols = ["v3", "v4", "v5"]
        if "v6" in multi:
            version_cols.append("v6")
        if "v7" in multi:
            version_cols.append("v7")
        header = " | ".join(version_cols)
        sep = " | ".join(["---:"] * len(version_cols))
        lines.extend([
            "---",
            "",
            f"## Version Comparison (v3 → v4 → v5"
            + (" → v6" if "v6" in multi else "")
            + (" → v7" if "v7" in multi else "")
            + ")",
            "",
            f"| Metric | {header} |",
            f"| :--- | {sep} |",
        ])
        pass_row = " | ".join(
            str(multi[v].get("total_pass_signals", "—")) for v in version_cols
        )
        lines.append(f"| PASS signals | {pass_row} |")
        ft_parts = []
        for v in version_cols:
            m = multi[v]
            ft_parts.append(
                f"{m['follow_through_count']} ({m['follow_through_rate']}%)"
            )
        lines.append(f"| Follow-through | {' | '.join(ft_parts)} |")
        if any(multi[v].get("total_watch_signals") is not None for v in version_cols):
            watch_parts = []
            for v in version_cols:
                w = multi[v].get("total_watch_signals")
                watch_parts.append(str(w) if w is not None else "—")
            lines.append(f"| WATCH signals | {' | '.join(watch_parts)} |")
        lines.append("")
    lines.extend([
        "---",
        "",
        "## Follow-Through Criteria",
        "",
        "**Follow-through (good):** any of:",
        "- T+15 close ≥ +5% vs entry close",
        "- MFE ≥ 8% within T+15 AND T+15 close ≥ 0%",
        "- Target hit within T+15 (stop/target path validation)",
        "- Never closed below entry through T+15 AND T+15 close ≥ +3%",
        "",
        "**No follow-through (fail/choppy):** any of:",
        "- T+15 close < 0%",
        "- MAE < −8% AND T+15 close < +3%",
        "- Stopped out within T+10",
        "- Max drawdown < −10% vs entry",
        "- Underwater >50% of T+15 sessions",
        "",
        "---",
        "",
        "## Cohort Summary",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Total PASS signals | {summary['total_pass_signals']} |",
        f"| WATCH signals | {summary.get('total_watch_signals', 0)} |",
        f"| Follow-through | {summary['follow_through_count']} ({summary['follow_through_rate']}%) |",
        f"| No follow-through | {summary['no_follow_through_count']} ({summary['no_follow_through_rate']}%) |",
        f"| Mixed | {summary['mixed_count']} |",
        f"| Avg T+15 (all signals) | {summary.get('avg_t15_all', 'n/a')}% |",
        f"| Avg MFE (all signals) | {summary.get('avg_mfe_all', 'n/a')}% |",
        "",
        "### Pass path distribution",
        "",
    ])
    for path, cnt in summary.get("pass_path_counts", {}).items():
        lines.append(f"- **{path}**: {cnt}")

    lines.extend(["", "### Root cause categories (failures only)", ""])
    for cause, cnt in summary.get("root_cause_counts", {}).items():
        lines.append(f"- **{cause}**: {cnt}")

    is_v5 = meta.get("analysis_version") == "v5"
    is_v6 = meta.get("analysis_version") == "v6"
    is_v7 = meta.get("analysis_version") == "v7"
    if is_v7:
        checklist_rel = V7_FIX_CHECKLIST.relative_to(ROOT).as_posix()
        lines.extend([
            "",
            "---",
            "",
            "## Implemented Filters (v7)",
            "",
            f"All items below are **implemented** in `src/breakout_scanner.py` + "
            f"`src/breakout_evidence.py` "
            f"(see [`{checklist_rel}`]({checklist_rel})).",
            "",
        ])
        for filt in report.get("implemented_filters") or _v7_implemented_filters():
            ref = filt["checklist_ref"]
            lines.append(
                f"- [x] **{ref}** — {filt['description']} "
                f"([checklist]({checklist_rel}))"
            )
    elif is_v6:
        checklist_rel = V6_FIX_CHECKLIST.relative_to(ROOT).as_posix()
        lines.extend([
            "",
            "---",
            "",
            "## Implemented Filters (v6)",
            "",
            f"All items below are **implemented** in `src/breakout_scanner.py` "
            f"(see [`{checklist_rel}`]({checklist_rel})).",
            "",
        ])
        for filt in report.get("implemented_filters") or _v6_implemented_filters():
            ref = filt["checklist_ref"]
            lines.append(
                f"- [x] **{ref}** — {filt['description']} "
                f"([checklist]({checklist_rel}))"
            )
    elif is_v5:
        checklist_rel = V5_FIX_CHECKLIST.relative_to(ROOT).as_posix()
        lines.extend([
            "",
            "---",
            "",
            "## Implemented Filters (v5)",
            "",
            f"All items below are **implemented** in `src/breakout_scanner.py` "
            f"(see [`{checklist_rel}`]({checklist_rel})).",
            "",
        ])
        for filt in report.get("implemented_filters") or _v5_implemented_filters():
            ref = filt["checklist_ref"]
            lines.append(
                f"- [x] **{ref}** — {filt['description']} "
                f"([checklist]({checklist_rel}))"
            )
    else:
        lines.extend(["", "---", "", "## Suggested Code Fixes", ""])
        for i, fix in enumerate(report.get("suggested_fixes") or [], 1):
            lines.append(
                f"{i}. **{fix['area']}** — {fix['fix']} ({fix['signals_affected']} failure signals)"
            )
        if not report.get("suggested_fixes"):
            lines.append("- No dominant failure pattern; cohort too small or evenly distributed.")

    lines.extend(["", "---", "", "## Symbol Universe", ""])
    lines.append(f"Small-cap ({meta['small_cap_count']}): {', '.join(meta['small_cap_symbols'])}")
    lines.append("")
    lines.append(f"Micro-cap ({meta['micro_cap_count']}): {', '.join(meta['micro_cap_symbols'])}")

    lines.extend(["", "---", "", "## Per-Signal Detail", ""])
    for sig in report["signals"]:
        ft = sig["follow_through"]
        lines.extend([
            f"### {sig['symbol']} — {sig['signal_date']} ({ft['label']})",
            "",
            f"- **Tier:** {sig['tier_label']}",
            f"- **Metrics:** chg {sig['metrics'].get('pct_change')}% vol {sig['metrics'].get('vol_mult')}x "
            f"RSI {sig['metrics'].get('rsi_val')} ADX {sig['metrics'].get('adx_val')}",
            f"- **Pass paths:** {', '.join(sig['metrics'].get('pass_paths') or []) or 'standard'}",
            f"- **T+15:** {ft.get('t15_vs_entry_pct', 'n/a')}% | MFE {ft.get('mfe_pct')}% | MAE {ft.get('mae_pct')}%",
            f"- **Follow reasons:** {'; '.join(ft['follow_reasons']) or '—'}",
            f"- **Fail reasons:** {'; '.join(ft['fail_reasons']) or '—'}",
        ])
        if sig.get("root_causes"):
            lines.append(f"- **Root causes:** {', '.join(sig['root_causes'])}")
        feat = sig.get("features") or {}
        if feat:
            lines.append(
                f"- **Pre-signal:** cum_ret_t10_t1 {feat.get('cum_ret_t10_t1')}% | "
                f"ADX {'rising' if feat.get('adx_rising') else 'falling'} | "
                f"prior_spike {feat.get('prior_spike_gt_8pct')} | "
                f"dist_20d_high {feat.get('dist_from_20d_high_pct')}%"
            )
        lines.append("")

    if report.get("fetch_skipped"):
        lines.extend(["", "## Skipped", ""])
        for s in report["fetch_skipped"]:
            lines.append(f"- {s}")

    lines.extend([
        "",
        "---",
        "",
        "## Methodology",
        "",
        "Daily replay of v3 `evaluate_bars_as_of` (pass_paths + pre_signal_validation) on cached "
        "Yahoo 6m bars. Forward path built from signal close entry. Root causes are multi-label "
        "heuristics from pass path, pre-signal features, and forward path.",
        "",
        "*Educational analysis only; not investment advice.*",
    ])
    return "\n".join(lines)


def run_analysis(
    *,
    universe_path: Path,
    range_str: str = "6m",
    baseline: dict[str, Any] | None = None,
    multi_baselines: dict[str, dict[str, Any]] | None = None,
    report_title: str | None = None,
    analysis_version: str | None = None,
) -> dict[str, Any]:
    universe_payload = json.loads(universe_path.read_text(encoding="utf-8"))
    stocks = universe_payload["stocks"]

    data_by_symbol = load_cached_universe_history(stocks, range_str=range_str)
    skipped = [s["symbol"] for s in stocks if s["symbol"] not in data_by_symbol]

    per_stock_reports: dict[str, Any] = {}
    raw_signals: list[dict[str, Any]] = []
    raw_watch_signals: list[dict[str, Any]] = []

    for entry in stocks:
        sym = entry["symbol"]
        if sym not in data_by_symbol:
            continue
        stock_report = replay_stock(sym, data_by_symbol[sym])
        per_stock_reports[sym] = stock_report
        for sig in stock_report.get("signals") or []:
            metrics = (sig.get("prediction") or {}).get("metrics") or {}
            raw_signals.append({
                "symbol": sym,
                "tier_label": entry.get("tier_label"),
                "tier_key": entry.get("tier_key"),
                "signal_date": sig.get("signal_date"),
                "bar_idx": sig.get("bar_idx"),
                "signal_tier": "PASS",
                "metrics": metrics,
                "outcome": sig.get("outcome") or {},
            })
        for sig in stock_report.get("watch_signals") or []:
            metrics = (sig.get("prediction") or {}).get("metrics") or {}
            raw_watch_signals.append({
                "symbol": sym,
                "tier_label": entry.get("tier_label"),
                "tier_key": entry.get("tier_key"),
                "signal_date": sig.get("signal_date"),
                "bar_idx": sig.get("bar_idx"),
                "signal_tier": "WATCH",
                "metrics": metrics,
                "outcome": sig.get("outcome") or {},
            })

    raw_signals.sort(key=lambda x: (x["symbol"], x["bar_idx"]))

    signals_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sig in raw_signals:
        signals_by_symbol[sig["symbol"]].append(sig)

    analyzed: list[dict[str, Any]] = []
    date_first: str | None = None
    date_last: str | None = None

    for sig in raw_signals:
        sym = sig["symbol"]
        stock_data = data_by_symbol[sym]
        df = stock_data["df"]
        dates = stock_data["dates"]
        bar_idx = sig["bar_idx"]
        entry = sig["metrics"].get("latest_price") or df["close"][bar_idx]

        daily_path, dip_summary = build_forward_path(df, bar_idx, entry)
        follow = classify_follow_through(
            daily_path=daily_path,
            dip_summary=dip_summary,
            outcome=sig["outcome"],
        )
        features = compute_pre_signal_features(df, bar_idx) or {}
        root_causes = categorize_root_causes(
            sig,
            features=features,
            follow=follow,
            signals_by_symbol=signals_by_symbol,
            df=df,
        )

        if dates:
            if date_first is None or dates[0] < date_first:
                date_first = dates[0]
            if date_last is None or dates[-1] > date_last:
                date_last = dates[-1]

        analyzed.append({
            **sig,
            "follow_through": follow,
            "features": features,
            "root_causes": root_causes,
            "daily_path": daily_path,
            "dip_summary": dip_summary,
        })

    follow_through = [s for s in analyzed if s["follow_through"]["label"] == "follow_through"]
    no_follow = [s for s in analyzed if s["follow_through"]["label"] == "no_follow_through"]
    mixed = [s for s in analyzed if s["follow_through"]["label"] == "mixed"]
    total = len(analyzed)

    root_cause_counter: Counter = Counter()
    for s in no_follow + mixed:
        for c in s.get("root_causes") or []:
            root_cause_counter[c] += 1

    pass_path_counter: Counter = Counter()
    for s in analyzed:
        paths = s["metrics"].get("pass_paths") or []
        if not paths:
            pass_path_counter["standard"] += 1
        for p in paths:
            pass_path_counter[p] += 1

    t15_vals = [s["follow_through"]["t15_vs_entry_pct"] for s in analyzed if s["follow_through"].get("t15_vs_entry_pct") is not None]
    mfe_vals = [s["follow_through"]["mfe_pct"] for s in analyzed if s["follow_through"].get("mfe_pct") is not None]

    commit = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        if r.stdout.strip():
            commit = r.stdout.strip()
    except OSError:
        pass

    small_syms = [s["symbol"] for s in stocks if s.get("tier_key") == "SMALL_CAP_100"]
    micro_syms = [s["symbol"] for s in stocks if s.get("tier_key") == "MICRO_CAP_250"]

    report = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "commit": commit,
            "report_title": report_title,
            "universe_path": str(universe_path),
            "range": range_str,
            "universe_total": len(stocks),
            "stocks_replayed": len(data_by_symbol),
            "small_cap_count": len(small_syms),
            "micro_cap_count": len(micro_syms),
            "small_cap_symbols": small_syms,
            "micro_cap_symbols": micro_syms,
            "date_range": {"first": date_first, "last": date_last},
        },
        "classification_rules": {
            "follow_through": [
                "t15_close_ge_5pct",
                "mfe_ge_8_and_t15_nonneg",
                "target_hit_within_t15",
                "never_below_entry_and_t15_ge_3pct",
            ],
            "no_follow_through": [
                "t15_close_lt_0",
                "mae_lt_-8_and_t15_lt_3",
                "stopped_out_within_t10",
                "max_dd_lt_-10",
                "underwater_most_t15",
            ],
        },
        "summary": {
            "total_pass_signals": total,
            "total_watch_signals": len(raw_watch_signals),
            "follow_through_count": len(follow_through),
            "no_follow_through_count": len(no_follow),
            "mixed_count": len(mixed),
            "follow_through_rate": round(100.0 * len(follow_through) / total, 2) if total else 0,
            "no_follow_through_rate": round(100.0 * len(no_follow) / total, 2) if total else 0,
            "avg_t15_all": round(statistics.mean(t15_vals), 2) if t15_vals else None,
            "avg_mfe_all": round(statistics.mean(mfe_vals), 2) if mfe_vals else None,
            "pass_path_counts": dict(pass_path_counter.most_common()),
            "root_cause_counts": dict(root_cause_counter.most_common()),
        },
        "fetch_skipped": skipped,
        "per_stock_signal_counts": {
            sym: per_stock_reports[sym].get("signal_count", 0)
            for sym in sorted(per_stock_reports.keys())
        },
        "per_stock_watch_counts": {
            sym: per_stock_reports[sym].get("watch_signal_count", 0)
            for sym in sorted(per_stock_reports.keys())
        },
        "signals": analyzed,
        "watch_signals": raw_watch_signals,
    }
    if analysis_version == "v7":
        report["meta"]["analysis_version"] = "v7"
        report["implemented_filters"] = _v7_implemented_filters()
    elif analysis_version == "v6":
        report["meta"]["analysis_version"] = "v6"
        report["implemented_filters"] = _v6_implemented_filters()
    elif analysis_version == "v5":
        report["meta"]["analysis_version"] = "v5"
        report["implemented_filters"] = _v5_implemented_filters()
    else:
        report["suggested_fixes"] = _suggest_fixes(root_cause_counter, no_follow + mixed)

    if baseline:
        after = report["summary"]
        report["baseline_comparison"] = {
            "baseline_label": baseline.get("label", "v3 pre-fix"),
            "baseline": {
                "total_pass_signals": baseline["total_pass_signals"],
                "follow_through_count": baseline["follow_through_count"],
                "follow_through_rate": baseline["follow_through_rate"],
                "no_follow_through_count": baseline["no_follow_through_count"],
                "no_follow_through_rate": baseline["no_follow_through_rate"],
                "mixed_count": baseline.get("mixed_count"),
            },
            "after": {
                "total_pass_signals": after["total_pass_signals"],
                "total_watch_signals": after.get("total_watch_signals", 0),
                "follow_through_count": after["follow_through_count"],
                "follow_through_rate": after["follow_through_rate"],
                "no_follow_through_count": after["no_follow_through_count"],
                "no_follow_through_rate": after["no_follow_through_rate"],
                "mixed_count": after["mixed_count"],
            },
            "delta": {
                "total_pass_signals": after["total_pass_signals"] - baseline["total_pass_signals"],
                "follow_through_count": after["follow_through_count"] - baseline["follow_through_count"],
                "follow_through_rate": round(
                    after["follow_through_rate"] - baseline["follow_through_rate"], 2,
                ),
                "no_follow_through_count": (
                    after["no_follow_through_count"] - baseline["no_follow_through_count"]
                ),
            },
        }
    if multi_baselines:
        after = report["summary"]
        report["multi_baseline_comparison"] = {
            "v3": {
                "total_pass_signals": multi_baselines["v3"]["total_pass_signals"],
                "follow_through_count": multi_baselines["v3"]["follow_through_count"],
                "follow_through_rate": multi_baselines["v3"]["follow_through_rate"],
            },
            "v4": {
                "total_pass_signals": multi_baselines["v4"]["total_pass_signals"],
                "follow_through_count": multi_baselines["v4"]["follow_through_count"],
                "follow_through_rate": multi_baselines["v4"]["follow_through_rate"],
            },
            "v5": {
                "total_pass_signals": multi_baselines["v5"]["total_pass_signals"],
                "total_watch_signals": multi_baselines["v5"].get("total_watch_signals"),
                "follow_through_count": multi_baselines["v5"]["follow_through_count"],
                "follow_through_rate": multi_baselines["v5"]["follow_through_rate"],
            },
        }
        v6_bl = multi_baselines.get("v6")
        if isinstance(v6_bl, dict):
            report["multi_baseline_comparison"]["v6"] = {
                "total_pass_signals": v6_bl["total_pass_signals"],
                "total_watch_signals": v6_bl.get("total_watch_signals"),
                "follow_through_count": v6_bl["follow_through_count"],
                "follow_through_rate": v6_bl["follow_through_rate"],
            }
        elif v6_bl is True:
            report["multi_baseline_comparison"]["v6"] = {
                "total_pass_signals": after["total_pass_signals"],
                "total_watch_signals": after.get("total_watch_signals", 0),
                "follow_through_count": after["follow_through_count"],
                "follow_through_rate": after["follow_through_rate"],
            }
        if multi_baselines.get("v7"):
            report["multi_baseline_comparison"]["v7"] = {
                "total_pass_signals": after["total_pass_signals"],
                "total_watch_signals": after.get("total_watch_signals", 0),
                "follow_through_count": after["follow_through_count"],
                "follow_through_rate": after["follow_through_rate"],
            }
        elif analysis_version not in ("v6", "v7"):
            report["multi_baseline_comparison"]["v5"].update({
                "total_pass_signals": after["total_pass_signals"],
                "total_watch_signals": after.get("total_watch_signals", 0),
                "follow_through_count": after["follow_through_count"],
                "follow_through_rate": after["follow_through_rate"],
            })
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="v3 trend follow-through analysis")
    ap.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    ap.add_argument("--range", dest="range_str", default="6m")
    ap.add_argument(
        "--output-md",
        type=Path,
        default=OUT_DIR / "v3_100stock_trend_analysis_report.md",
    )
    ap.add_argument(
        "--output-json",
        type=Path,
        default=OUT_DIR / "v3_100stock_trend_analysis.json",
    )
    ap.add_argument(
        "--with-baseline",
        action="store_true",
        help="Include v3 pre-fix baseline comparison (48 signals, 31.25%% follow-through)",
    )
    ap.add_argument(
        "--v5",
        action="store_true",
        help="v5 run: multi-baseline v3/v4/v5 comparison and default v5 output paths",
    )
    ap.add_argument(
        "--v7",
        action="store_true",
        help="v7 run: multi-baseline v3/v4/v5/v6/v7 comparison and default v7 output paths",
    )
    ap.add_argument(
        "--v6",
        action="store_true",
        help="v6 run: multi-baseline v3/v4/v5/v6 comparison and default v6 output paths",
    )
    args = ap.parse_args(argv)

    if not args.universe.is_file():
        print(f"Missing universe file: {args.universe}", file=sys.stderr)
        return 1

    if "v7" in args.output_md.stem.lower():
        args.v7 = True
    elif "v6" in args.output_md.stem.lower():
        args.v6 = True
    elif "v5" in args.output_md.stem.lower():
        args.v5 = True

    if args.v7:
        args.with_baseline = True
        args.output_md = OUT_DIR / "v3_100stock_trend_analysis_v7_report.md"
        args.output_json = OUT_DIR / "v3_100stock_trend_analysis_v7.json"
        if args.universe == DEFAULT_UNIVERSE:
            args.universe = OUT_DIR / "universe_100.json"
    elif args.v6:
        args.with_baseline = True
        args.output_md = OUT_DIR / "v3_100stock_trend_analysis_v6_report.md"
        args.output_json = OUT_DIR / "v3_100stock_trend_analysis_v6.json"
        if args.universe == DEFAULT_UNIVERSE:
            args.universe = OUT_DIR / "universe_100.json"
    elif args.v5:
        args.with_baseline = True
        args.output_md = OUT_DIR / "v3_100stock_trend_analysis_v5_report.md"
        args.output_json = OUT_DIR / "v3_100stock_trend_analysis_v5.json"
        if args.universe == DEFAULT_UNIVERSE:
            args.universe = OUT_DIR / "universe_100.json"

    report = run_analysis(
        universe_path=args.universe,
        range_str=args.range_str,
        baseline=(
            V6_BASELINE if args.v7 else (
                V5_BASELINE if args.v6 else (
                    V4_BASELINE if args.v5 else (V3_BASELINE if args.with_baseline else None)
                )
            )
        ),
        multi_baselines=(
            {"v3": V3_BASELINE, "v4": V4_BASELINE, "v5": V5_BASELINE, "v6": V6_BASELINE, "v7": True}
            if args.v7
            else (
                {"v3": V3_BASELINE, "v4": V4_BASELINE, "v5": V5_BASELINE, "v6": True}
                if args.v6
                else ({"v3": V3_BASELINE, "v4": V4_BASELINE} if args.v5 else None)
            )
        ),
        report_title=(
            "v3 Breakout Trend Follow-Through Analysis v7 (100-Stock Cohort, Post-Fix)"
            if args.v7
            else (
                "v3 Breakout Trend Follow-Through Analysis v6 (100-Stock Cohort, Post-Fix)"
                if args.v6
                else (
                    "v3 Breakout Trend Follow-Through Analysis v5 (100-Stock Cohort, Post-Fix)"
                    if args.v5
                    else (
                        "v3 Breakout Trend Follow-Through Analysis v4 (100-Stock Cohort, Post-Fix)"
                        if args.with_baseline
                        else None
                    )
                )
            )
        ),
        analysis_version="v7" if args.v7 else ("v6" if args.v6 else ("v5" if args.v5 else None)),
    )

    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(build_markdown(report), encoding="utf-8")

    # Slim JSON for summary (omit daily_path per signal to keep size manageable)
    slim_signals = []
    for s in report["signals"]:
        slim = {k: v for k, v in s.items() if k != "daily_path"}
        slim_signals.append(slim)
    slim_report = {**report, "signals": slim_signals}
    args.output_json.write_text(json.dumps(slim_report, indent=2, default=str), encoding="utf-8")

    s = report["summary"]
    print(f"PASS signals: {s['total_pass_signals']}")
    if s.get("total_watch_signals"):
        print(f"WATCH signals: {s['total_watch_signals']}")
    print(f"Follow-through: {s['follow_through_count']} ({s['follow_through_rate']}%)")
    print(f"No follow-through: {s['no_follow_through_count']} ({s['no_follow_through_rate']}%)")
    print(f"Report: {args.output_md}")
    print(f"JSON: {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
