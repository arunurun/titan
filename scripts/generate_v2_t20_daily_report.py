#!/usr/bin/env python3
"""Generate T+20 daily price path report for all v2 PASS signals."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from breakout_backtest import load_cached_universe_history, replay_stock  # noqa: E402
from breakout_scanner import (  # noqa: E402
    ADX_HARD_FLOOR,
    FILTERS,
    PCT_CHANGE_MAX_NORMAL,
    PCT_CHANGE_MAX_POWER_GAP,
    PCT_CHANGE_MIN,
    RSI_MAX_NORMAL,
    RSI_MIN,
    evaluate_bars_as_of,
)

T20_HORIZON = 20


def evaluate_bars_v1(df, as_of_idx: int, tier_name: str) -> dict:
    """Strict v1 rules: no alternate pass paths."""
    filt = FILTERS[tier_name]
    vol_thresh = filt["vol_mult"]
    n = as_of_idx + 1
    if n < 50:
        return {"passed": False, "fail_reason": "insufficient_data"}

    result = evaluate_bars_as_of(df, as_of_idx, tier_name)
    price = result["latest_price"]
    pct = result["pct_change"]
    vol_mult = result["vol_mult"]
    rsi = result["rsi_val"]
    adx = result["adx_val"]
    sma50 = result["sma50_last"]
    target_gain = result["target_gain"]

    if price < filt["min_price"]:
        return {**result, "passed": False, "fail_reason": "min_price"}
    if pct < PCT_CHANGE_MIN or pct > PCT_CHANGE_MAX_NORMAL:
        return {**result, "passed": False, "fail_reason": "pct_change"}
    if price < sma50:
        return {**result, "passed": False, "fail_reason": "SMA50"}
    if vol_mult < vol_thresh:
        return {**result, "passed": False, "fail_reason": "vol"}
    if not (RSI_MIN <= rsi <= RSI_MAX_NORMAL):
        return {**result, "passed": False, "fail_reason": "RSI"}
    if adx < ADX_HARD_FLOOR:
        return {**result, "passed": False, "fail_reason": "ADX"}
    if target_gain < 8.0:
        return {**result, "passed": False, "fail_reason": "target_gain"}
    return {**result, "passed": True, "fail_reason": None}


def _v1_block_explanation(fail_reason: str | None, metrics: dict) -> str:
    if not fail_reason:
        return "Would pass v1 (standard path)"
    if fail_reason == "pct_change":
        pct = metrics.get("pct_change", 0)
        if pct > PCT_CHANGE_MAX_NORMAL:
            return f"Daily change +{pct:.2f}% exceeds v1 ceiling ({PCT_CHANGE_MAX_NORMAL}%)"
        return f"Daily change +{pct:.2f}% below v1 floor ({PCT_CHANGE_MIN}%)"
    if fail_reason == "vol":
        thresh = metrics.get("vol_mult_threshold", 3.0)
        return f"Single-day vol {metrics.get('vol_mult', 0):.2f}x < tier threshold {thresh}x"
    if fail_reason == "SMA50":
        return f"Price {metrics.get('latest_price')} below SMA50 {metrics.get('sma50_last')}"
    if fail_reason == "RSI":
        return f"RSI {metrics.get('rsi_val')} outside 50–70 (no rsi_hot path)"
    if fail_reason == "ADX":
        return f"ADX {metrics.get('adx_val')} < {ADX_HARD_FLOOR} (no adx_soft band)"
    if fail_reason == "target_gain":
        return f"Est. target gain {metrics.get('target_gain')}% < 8%"
    return f"Blocked by {fail_reason}"


def _v2_pass_explanation(pass_paths: list[str]) -> str:
    if not pass_paths:
        return "Standard path — all v1 filters satisfied"
    labels = {
        "power_gap": f"power_gap: +{PCT_CHANGE_MAX_NORMAL}–{PCT_CHANGE_MAX_POWER_GAP}% day allowed",
        "vol_continuation_cum3d": "vol_continuation: 3-session cumulative vol ≥ tier threshold",
        "vol_continuation_prior_spike": "vol_continuation: micro-cap 2.5x after prior spike",
        "sma20_reclaim": "sma20_reclaim: price > SMA20 with vol ≥ 5x despite below SMA50",
        "rsi_hot": "rsi_hot: RSI 70–75 with vol > 5x",
        "adx_soft": "adx_soft: ADX 20–25 with strong vol + above SMA50 + positive day",
    }
    return "; ".join(labels.get(p, p) for p in pass_paths)


def _build_daily_path(
    df: dict,
    dates: list[str],
    signal_idx: int,
    entry: float,
    horizon: int = T20_HORIZON,
) -> tuple[list[dict], dict]:
    """Build T+1..T+N daily rows and dip summary."""
    closes = df["close"]
    n = len(closes)
    rows: list[dict] = []
    first_dip_day: int | None = None
    max_dd_day: int | None = None
    max_dd_pct: float | None = None

    for d in range(1, horizon + 1):
        idx = signal_idx + d
        if idx >= n:
            rows.append({
                "day": d,
                "date": None,
                "close": None,
                "day_chg_pct": None,
                "vs_entry_pct": None,
                "below_entry": None,
                "insufficient_bars": True,
            })
            continue

        close = closes[idx]
        prev_close = closes[idx - 1]
        day_chg = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
        vs_entry = (close / entry - 1.0) * 100.0
        below = close < entry

        if below and first_dip_day is None:
            first_dip_day = d
        if max_dd_pct is None or vs_entry < max_dd_pct:
            max_dd_pct = vs_entry
            max_dd_day = d

        rows.append({
            "day": d,
            "date": dates[idx] if idx < len(dates) else None,
            "close": round(close, 2),
            "day_chg_pct": round(day_chg, 4),
            "vs_entry_pct": round(vs_entry, 4),
            "below_entry": below,
            "insufficient_bars": False,
        })

    best_day = max(
        (r for r in rows if r.get("vs_entry_pct") is not None),
        key=lambda r: r["vs_entry_pct"],
        default=None,
    )
    summary = {
        "sessions_available": sum(1 for r in rows if not r.get("insufficient_bars")),
        "first_dip_day": first_dip_day,
        "first_dip_vs_entry_pct": next(
            (r["vs_entry_pct"] for r in rows if r["day"] == first_dip_day), None
        ),
        "max_drawdown_day": max_dd_day,
        "max_drawdown_vs_entry_pct": round(max_dd_pct, 4) if max_dd_pct is not None else None,
        "best_day": best_day["day"] if best_day else None,
        "best_vs_entry_pct": best_day["vs_entry_pct"] if best_day else None,
        "ever_below_entry": first_dip_day is not None,
        "final_t20_vs_entry_pct": rows[-1]["vs_entry_pct"] if rows and rows[-1].get("vs_entry_pct") is not None else None,
    }
    return rows, summary


def _collect_signals(universe: dict, data: dict) -> list[dict]:
    signals: list[dict] = []
    for entry in universe["stocks"]:
        sym = entry["symbol"]
        if sym not in data:
            continue
        stock_data = data[sym]
        tier_key = stock_data["tier_key"]
        rep = replay_stock(sym, stock_data)
        for sig in rep.get("signals") or []:
            idx = sig["bar_idx"]
            v2 = sig["prediction"]["metrics"]
            v1 = evaluate_bars_v1(stock_data["df"], idx, tier_key)
            outcome = sig.get("outcome") or {}
            entry_price = outcome.get("entry") or v2.get("latest_price")
            pass_paths = v2.get("pass_paths") or []
            path_label = ", ".join(pass_paths) if pass_paths else "standard"
            daily, dip = _build_daily_path(
                stock_data["df"], stock_data["dates"], idx, float(entry_price),
            )
            signals.append({
                "symbol": sym,
                "tier": entry.get("tier_label", stock_data["tier_label"]),
                "signal_date": sig["signal_date"],
                "bar_idx": idx,
                "entry": entry_price,
                "pass_path": path_label,
                "pass_paths": pass_paths,
                "v1_fail": v1.get("fail_reason"),
                "v1_blocked": _v1_block_explanation(v1.get("fail_reason"), v2),
                "v2_pass": _v2_pass_explanation(pass_paths),
                "pct_change": v2.get("pct_change"),
                "vol_mult": v2.get("vol_mult"),
                "daily_path": daily,
                "dip_summary": dip,
            })
    signals.sort(key=lambda s: (s["signal_date"], s["symbol"]))
    return signals


def _fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def _build_markdown(signals: list[dict]) -> str:
    below_count = sum(1 for s in signals if s["dip_summary"]["ever_below_entry"])
    dd_days = [s["dip_summary"]["max_drawdown_vs_entry_pct"] for s in signals if s["dip_summary"]["max_drawdown_vs_entry_pct"] is not None]
    best_days = [s["dip_summary"]["best_vs_entry_pct"] for s in signals if s["dip_summary"]["best_vs_entry_pct"] is not None]
    worst_dd = min(dd_days) if dd_days else None
    best_peak = max(best_days) if best_days else None
    best_signal = max(signals, key=lambda s: s["dip_summary"]["best_vs_entry_pct"] or -999)

    lines = [
        "# V2 PASS Signals — T+20 Daily Price Path Report",
        "",
        "**Branch:** `titanBacktest`  ",
        "**Universe:** 40-stock liquid cohort (Nifty Smallcap 100 + Microcap 250)  ",
        "**Horizon:** T+1 through T+20 trading sessions after signal close (entry at T)  ",
        f"**Signals:** {len(signals)} v2 PASS events",
        "",
        "---",
        "",
        "## Cohort Summary",
        "",
        f"| Metric | Value |",
        f"| :--- | ---: |",
        f"| Total signals | {len(signals)} |",
        f"| Closed below entry at least once (T+1–T+20) | {below_count} ({round(100*below_count/len(signals), 1)}%) |",
        f"| Worst close drawdown vs entry | {_fmt_pct(worst_dd)} |",
        f"| Best peak close vs entry | {_fmt_pct(best_peak)} |",
        f"| Best peak signal | {best_signal['symbol']} {best_signal['signal_date']} ({_fmt_pct(best_signal['dip_summary']['best_vs_entry_pct'])}) |",
        "",
        "---",
        "",
    ]

    for i, sig in enumerate(signals, 1):
        dip = sig["dip_summary"]
        v1_block = sig["v1_fail"] or "—"
        lines.extend([
            f"## Stock {i}: {sig['symbol']} — PASS on {sig['signal_date']} @ ₹{sig['entry']}",
            "",
            f"**Tier:** {sig['tier']}  ",
            f"**pass_path:** `{sig['pass_path']}`  ",
            f"**V1 blocked:** `{v1_block}` — {sig['v1_blocked']}  ",
            f"**V2 pass:** {sig['v2_pass']}",
            "",
        ])

        if dip["ever_below_entry"]:
            lines.append(
                f"**Dip below entry:** first on T+{dip['first_dip_day']} "
                f"({_fmt_pct(dip['first_dip_vs_entry_pct'])}); "
                f"max drawdown T+{dip['max_drawdown_day']} ({_fmt_pct(dip['max_drawdown_vs_entry_pct'])})"
            )
        else:
            lines.append("**Dip below entry:** Never (close stayed ≥ entry through available window)")
        if dip["best_day"]:
            lines.append(
                f"**Best day:** T+{dip['best_day']} ({_fmt_pct(dip['best_vs_entry_pct'])})"
            )
        lines.extend([
            "",
            "| Day | Date | Close | Day chg% | vs Entry% | Below entry? |",
            "| :--- | :--- | ---: | ---: | ---: | :---: |",
        ])

        for row in sig["daily_path"]:
            if row.get("insufficient_bars"):
                lines.append(f"| T+{row['day']} | — | — | — | — | — |")
                continue
            be = "Y" if row["below_entry"] else ""
            flags = []
            if dip["ever_below_entry"]:
                if row["day"] == dip.get("first_dip_day"):
                    flags.append("first dip")
                if row["day"] == dip.get("max_drawdown_day"):
                    flags.append("max DD")
            if row["day"] == dip.get("best_day"):
                flags.append("best")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"| T+{row['day']} | {row['date']} | {row['close']:.2f} | "
                f"{row['day_chg_pct']:+.2f}% | {row['vs_entry_pct']:+.2f}% | {be}{flag_str} |"
            )
        lines.extend(["", "---", ""])

    lines.extend([
        "",
        "*Educational backtest only. Entry = signal-day close. Uses cached Yahoo bars in `data/cache/breakout_yahoo/backtest/`.*",
    ])
    return "\n".join(lines)


def main() -> int:
    out_dir = ROOT / "output" / "breakoutcheck"
    universe = json.loads((out_dir / "universe_40.json").read_text(encoding="utf-8"))
    data = load_cached_universe_history(universe["stocks"], range_str="6m")
    signals = _collect_signals(universe, data)

    md_path = out_dir / "v2_pass_signals_t20_daily_report.md"
    json_path = out_dir / "v2_pass_signals_t20_daily_report.json"

    cohort = {
        "signal_count": len(signals),
        "below_entry_count": sum(1 for s in signals if s["dip_summary"]["ever_below_entry"]),
        "avg_final_t20": round(
            statistics.mean(
                s["dip_summary"]["final_t20_vs_entry_pct"]
                for s in signals
                if s["dip_summary"]["final_t20_vs_entry_pct"] is not None
            ),
            2,
        ) if signals else None,
        "worst_drawdown_pct": min(
            (s["dip_summary"]["max_drawdown_vs_entry_pct"] for s in signals
             if s["dip_summary"]["max_drawdown_vs_entry_pct"] is not None),
            default=None,
        ),
        "best_peak_pct": max(
            (s["dip_summary"]["best_vs_entry_pct"] for s in signals
             if s["dip_summary"]["best_vs_entry_pct"] is not None),
            default=None,
        ),
    }

    md_path.write_text(_build_markdown(signals), encoding="utf-8")
    json_path.write_text(json.dumps({"cohort": cohort, "signals": signals}, indent=2), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Signals: {len(signals)}, dipped below entry: {cohort['below_entry_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
