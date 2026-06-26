#!/usr/bin/env python3
"""Generate T-15..T-1, T, T+1..T+15 daily window report for v2 PASS signals."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from breakout_scanner import (  # noqa: E402
    _parse_yahoo_chart_response,
    bar_dates_from_df,
)

OUT_DIR = ROOT / "output" / "breakoutcheck"
CACHE_DIR = ROOT / "data" / "cache" / "breakout_yahoo" / "backtest"
SIGNALS_JSON = OUT_DIR / "v2_pass_signals_report.json"
PRE_WINDOW = 15
POST_WINDOW = 15


def load_backtest_bars(symbol: str, range_str: str = "6m") -> dict[str, Any] | None:
    ticker = f"{symbol}.NS"
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = CACHE_DIR / f"{safe}_{range_str}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _parse_yahoo_chart_response(data)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, OSError):
        return None


def _day_chg_pct(closes: list[float], idx: int) -> float | None:
    if idx <= 0 or idx >= len(closes):
        return None
    prev = closes[idx - 1]
    if not prev:
        return None
    return (closes[idx] / prev - 1.0) * 100.0


def _vs_signal_pct(close: float, signal_close: float) -> float | None:
    if not signal_close:
        return None
    return (close / signal_close - 1.0) * 100.0


def _fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def classify_pre_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify T-15..T-1 price path as rising / falling / flat."""
    pre = [r for r in rows if r["offset"] < 0 and r.get("close") is not None]
    if len(pre) < 2:
        return {"label": "unknown", "cum_pct_t15_to_t1": None, "summary": "Insufficient pre-signal bars"}

    first = pre[0]
    last = pre[-1]
    cum = _vs_signal_pct(last["close"], first["close"])
    if cum is None:
        return {"label": "unknown", "cum_pct_t15_to_t1": None, "summary": "Could not compute pre-window return"}

    if cum >= 3.0:
        label = "rising"
    elif cum <= -3.0:
        label = "falling"
    else:
        label = "flat"

    return {
        "label": label,
        "cum_pct_t15_to_t1": round(cum, 4),
        "close_t15": first["close"],
        "close_t1": last["close"],
        "summary": f"{label.capitalize()} into signal ({_fmt_pct(cum)} T-15→T-1)",
    }


def classify_post_trend(rows: list[dict[str, Any]], entry: float) -> dict[str, Any]:
    """Classify T+1..T+15 path as winner / loser / choppy."""
    post = [r for r in rows if r["offset"] > 0 and r.get("close") is not None]
    if not post:
        return {"label": "unknown", "summary": "No forward bars available"}

    vs_vals = [r["vs_signal_pct"] for r in post if r.get("vs_signal_pct") is not None]
    final_row = post[-1]
    final_pct = final_row.get("vs_signal_pct")
    mfe = max(vs_vals) if vs_vals else None
    mae = min(vs_vals) if vs_vals else None
    below_days = sum(1 for r in post if r.get("close") is not None and r["close"] < entry)
    sign_changes = 0
    for i in range(1, len(vs_vals)):
        if vs_vals[i - 1] * vs_vals[i] < 0:
            sign_changes += 1

    if final_pct is not None and final_pct >= 5.0:
        label = "winner"
    elif final_pct is not None and final_pct <= -5.0:
        label = "loser"
    elif sign_changes >= 2 or (mfe is not None and mae is not None and (mfe - mae) >= 15.0):
        label = "choppy"
    elif final_pct is not None and final_pct > 0:
        label = "winner"
    elif final_pct is not None and final_pct < 0:
        label = "loser"
    else:
        label = "choppy"

    summary_parts = [
        f"final T+{final_row['offset']}: {_fmt_pct(final_pct)}",
    ]
    if mfe is not None:
        summary_parts.append(f"MFE {_fmt_pct(mfe)}")
    if mae is not None:
        summary_parts.append(f"MAE {_fmt_pct(mae)}")
    if below_days:
        summary_parts.append(f"below entry {below_days}/{len(post)}d")

    return {
        "label": label,
        "final_offset": final_row["offset"],
        "final_vs_signal_pct": final_pct,
        "mfe_vs_signal_pct": round(mfe, 4) if mfe is not None else None,
        "mae_vs_signal_pct": round(mae, 4) if mae is not None else None,
        "below_entry_days": below_days,
        "sign_changes": sign_changes,
        "summary": f"{label.capitalize()} — " + "; ".join(summary_parts),
    }


def build_window_rows(
    df: dict[str, Any],
    dates: list[str],
    signal_idx: int,
    signal_close: float,
    entry: float,
) -> list[dict[str, Any]]:
    closes = df["close"]
    n = len(closes)
    rows: list[dict[str, Any]] = []

    for offset in range(-PRE_WINDOW, POST_WINDOW + 1):
        idx = signal_idx + offset
        if idx < 0 or idx >= n:
            rows.append({
                "offset": offset,
                "date": None,
                "close": None,
                "day_chg_pct": None,
                "vs_signal_pct": None,
                "note": "insufficient bars",
                "insufficient_bars": True,
            })
            continue

        close = round(closes[idx], 2)
        day_chg = _day_chg_pct(closes, idx)
        vs_signal = _vs_signal_pct(close, signal_close)
        note = ""

        if offset == 0:
            note = "SIGNAL (pass day)"
        elif offset < 0 and offset == -1:
            note = "day before signal"
        elif offset > 0 and close < entry:
            note = "below entry"

        rows.append({
            "offset": offset,
            "date": dates[idx] if idx < len(dates) else None,
            "close": close,
            "day_chg_pct": round(day_chg, 4) if day_chg is not None else None,
            "vs_signal_pct": round(vs_signal, 4) if vs_signal is not None else None,
            "note": note,
            "insufficient_bars": False,
        })

    return rows


def _offset_label(offset: int) -> str:
    if offset == 0:
        return "T"
    if offset < 0:
        return f"T{offset}"
    return f"T+{offset}"


def build_signal_report(sig: dict[str, Any], df: dict[str, Any], dates: list[str]) -> dict[str, Any]:
    signal_date = sig["date"]
    try:
        signal_idx = dates.index(signal_date)
    except ValueError:
        return {
            **sig,
            "signal_date": signal_date,
            "error": f"Signal date {signal_date} not found in bar history",
            "daily_window": [],
            "pre_trend": {"label": "unknown", "summary": "date not in cache"},
            "post_trend": {"label": "unknown", "summary": "date not in cache"},
        }

    signal_close = df["close"][signal_idx]
    entry = float(sig.get("entry") or signal_close)
    rows = build_window_rows(df, dates, signal_idx, signal_close, entry)

    pre_trend = classify_pre_trend(rows)
    post_trend = classify_post_trend(rows, entry)

    return {
        "symbol": sig["symbol"],
        "tier": sig.get("tier"),
        "signal_date": signal_date,
        "entry": round(entry, 2),
        "signal_close": round(signal_close, 2),
        "bar_idx": signal_idx,
        "pct_change": sig.get("pct_change"),
        "vol_mult": sig.get("vol_mult"),
        "v1_fail": sig.get("v1_fail"),
        "v2_pass_paths": sig.get("v2_pass_paths") or [],
        "v2_pass": sig.get("v2_pass"),
        "t5": sig.get("t5"),
        "t10": sig.get("t10"),
        "t15": sig.get("t15"),
        "mfe": sig.get("mfe"),
        "mae": sig.get("mae"),
        "below_entry": sig.get("below_entry"),
        "pre_trend": pre_trend,
        "post_trend": post_trend,
        "daily_window": rows,
    }


def build_markdown(reports: list[dict[str, Any]]) -> str:
    pre_counts: dict[str, int] = {}
    post_counts: dict[str, int] = {}
    for r in reports:
        pre_counts[r["pre_trend"]["label"]] = pre_counts.get(r["pre_trend"]["label"], 0) + 1
        post_counts[r["post_trend"]["label"]] = post_counts.get(r["post_trend"]["label"], 0) + 1

    lines = [
        "# V2 PASS Signals — T-15 / T / T+15 Window Report",
        "",
        "**Cohort:** 40-stock liquid universe (Nifty Smallcap 100 + Microcap 250)  ",
        f"**Signals:** {len(reports)} v2 PASS events  ",
        f"**Window:** T-{PRE_WINDOW}..T-1 (pre), signal day T, T+1..T+{POST_WINDOW} (post)  ",
        "**Reference close:** signal-day close (T) for `vs signal close%`",
        "",
        "---",
        "",
        "## Cohort Trend Summary",
        "",
        "| Pre-trend (T-15→T-1) | Count |",
        "| :--- | ---: |",
    ]
    for label in ("rising", "flat", "falling", "unknown"):
        if label in pre_counts:
            lines.append(f"| {label} | {pre_counts[label]} |")

    lines.extend([
        "",
        "| Post-trend (T+1→T+15) | Count |",
        "| :--- | ---: |",
    ])
    for label in ("winner", "choppy", "loser", "unknown"):
        if label in post_counts:
            lines.append(f"| {label} | {post_counts[label]} |")

    lines.extend(["", "---", ""])

    for i, rep in enumerate(reports, 1):
        paths = ", ".join(rep.get("v2_pass_paths") or []) or "standard"
        lines.extend([
            f"## {i}. {rep['symbol']} — PASS {rep['signal_date']} @ ₹{rep['entry']}",
            "",
            f"**Tier:** {rep.get('tier', 'n/a')}  ",
            f"**V2 paths:** `{paths}`  ",
            f"**Pre-trend:** {rep['pre_trend']['summary']}  ",
            f"**Post-trend:** {rep['post_trend']['summary']}",
            "",
            "| Offset | Date | Close | Day chg% | vs signal close% | Note |",
            "| :--- | :--- | ---: | ---: | ---: | :--- |",
        ])

        if rep.get("error"):
            lines.extend([f"| — | — | — | — | — | {rep['error']} |", "", "---", ""])
            continue

        for row in rep["daily_window"]:
            if row.get("insufficient_bars"):
                lines.append(
                    f"| {_offset_label(row['offset'])} | — | — | — | — | insufficient bars |"
                )
                continue
            lines.append(
                f"| {_offset_label(row['offset'])} | {row['date']} | {row['close']:.2f} | "
                f"{_fmt_pct(row['day_chg_pct'])} | {_fmt_pct(row['vs_signal_pct'])} | "
                f"{row.get('note') or ''} |"
            )
        lines.extend(["", "---", ""])

    lines.append(
        "\n*Educational backtest only. Entry = signal-day close. "
        "Bars from `data/cache/breakout_yahoo/backtest/{SYMBOL}.NS_6m.json`.*"
    )
    return "\n".join(lines)


def main() -> int:
    payload = json.loads(SIGNALS_JSON.read_text(encoding="utf-8"))
    source_signals = payload.get("signals") or []
    reports: list[dict[str, Any]] = []
    errors: list[str] = []

    for sig in source_signals:
        sym = sig["symbol"]
        df = load_backtest_bars(sym)
        if df is None:
            errors.append(f"{sym}: cache missing")
            reports.append({
                **sig,
                "signal_date": sig["date"],
                "error": "cache missing",
                "daily_window": [],
                "pre_trend": {"label": "unknown", "summary": "cache missing"},
                "post_trend": {"label": "unknown", "summary": "cache missing"},
            })
            continue
        dates = bar_dates_from_df(df)
        reports.append(build_signal_report(sig, df, dates))

    reports.sort(key=lambda r: (r.get("signal_date") or "", r.get("symbol") or ""))

    md_path = OUT_DIR / "v2_pass_signals_t15_before_t15_after_report.md"
    json_path = OUT_DIR / "v2_pass_signals_t15_before_t15_after_report.json"

    cohort = {
        "signal_count": len(reports),
        "pre_window": PRE_WINDOW,
        "post_window": POST_WINDOW,
        "pre_trend_counts": {
            k: sum(1 for r in reports if r["pre_trend"]["label"] == k)
            for k in ("rising", "flat", "falling", "unknown")
        },
        "post_trend_counts": {
            k: sum(1 for r in reports if r["post_trend"]["label"] == k)
            for k in ("winner", "choppy", "loser", "unknown")
        },
        "cache_errors": errors,
        "avg_t15_vs_signal": round(
            statistics.mean(
                r["post_trend"]["final_vs_signal_pct"]
                for r in reports
                if r.get("post_trend", {}).get("final_vs_signal_pct") is not None
            ),
            2,
        ) if reports else None,
    }

    md_path.write_text(build_markdown(reports), encoding="utf-8")
    json_path.write_text(
        json.dumps({"cohort": cohort, "signals": reports}, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Signals: {len(reports)}")
    if errors:
        print(f"Cache errors: {len(errors)} — {', '.join(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
