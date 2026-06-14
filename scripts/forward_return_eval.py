#!/usr/bin/env python3
"""Forward-return measurement harness (read-only scoreboard).

Computes REALIZED FORWARD returns at +1 / +5 sessions AFTER a signal date from the
stored Supabase ``symbol_daily_features`` daily-return series. It never measures the
same-day (trailing) move: forward windows start strictly on the session that follows
the signal date.

Data caveat: ``symbol_daily_features`` stores trailing returns only
(``return_1d_pct`` is the close-to-close move realized ON that ``trade_date``;
``tape_extras.return_5d_pct`` is trailing). There is no raw close column, so the
forward path is reconstructed by compounding the per-symbol ``return_1d_pct`` series
of the sessions that come AFTER the signal date.

Public API (kept stable for importers, e.g. Worker C)::

    from forward_return_eval import evaluate
    result = evaluate(
        symbols=["CANBK", "ABB"],   # OR pass sectors=[...]
        start="2026-05-15",
        end="2026-06-12",
        horizons=(1, 5),            # forward sessions after the signal date
    )
    # result = {
    #   "params": {...},
    #   "per_symbol": {SYMBOL: {<metrics>}, ...},
    #   "per_observation": [{symbol, signal_date, forward_*_pct, ...}, ...],
    #   "cohort": {<aggregate metrics>},
    # }

CLI::

    python scripts/forward_return_eval.py --symbols CANBK,ABB --start 2026-05-15 --end 2026-06-12
    python scripts/forward_return_eval.py --sectors defence,ai,telecom --start 2026-05-15 --end 2026-06-12
    python scripts/forward_return_eval.py --preset twelve --start 2026-05-15 --end 2026-06-12 --json

Credentials are sourced from ``.env`` via ``src/config_loader.load_config``
(Supabase ``service_role``). This tool does NOT run live Breeze.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The 12 reviewed stocks (kept here for the --preset twelve convenience).
TWELVE_STOCKS: tuple[str, ...] = (
    "CANBK", "ABB", "INDIGO", "DIXON", "HINDPETRO", "DIVISLAB",
    "MAHABANK", "PNB", "CANFINHOME", "GARFIBRES", "EICHERMOT", "GREAVESCOT",
)

DEFAULT_HORIZONS: tuple[int, ...] = (1, 5)
_FETCH_PAGE = 1000


def _safe_float(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x


def _to_iso(d: str | date | datetime | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    return s or None


def _parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _fetch_feature_rows(
    *,
    symbols: Sequence[str] | None,
    sectors: Sequence[str] | None,
    start_iso: str,
    end_iso: str,
    exchange: str | None,
    cfg: Any = None,
) -> list[dict[str, Any]]:
    """Read-only paginated pull of (trade_date, symbol, return_1d_pct) over a window."""
    from config_loader import load_config
    from supabase import create_client

    cfg = cfg or load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    select_cols = "trade_date,symbol,exchange,sector,action_signal,return_1d_pct"

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        q = (
            client.table("symbol_daily_features")
            .select(select_cols)
            .gte("trade_date", start_iso)
            .lte("trade_date", end_iso)
        )
        if symbols:
            q = q.in_("symbol", [s.strip().upper() for s in symbols if s.strip()])
        if sectors:
            q = q.in_("sector", [s.strip().lower() for s in sectors if s.strip()])
        if exchange:
            q = q.eq("exchange", exchange.strip().upper())
        q = q.order("trade_date").range(offset, offset + _FETCH_PAGE - 1)
        batch = list(getattr(q.execute(), "data", None) or [])
        rows.extend(batch)
        if len(batch) < _FETCH_PAGE:
            break
        offset += _FETCH_PAGE
    return rows


def _series_by_symbol(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group rows into ascending-by-date daily-return series, deduped per trade_date."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        td = str(row.get("trade_date") or "").strip()[:10]
        if not sym or not td:
            continue
        grouped.setdefault(sym, {})[td] = row
    out: dict[str, list[dict[str, Any]]] = {}
    for sym, by_date in grouped.items():
        out[sym] = [by_date[d] for d in sorted(by_date)]
    return out


def _forward_metrics_for_signal(
    series: list[dict[str, Any]],
    signal_idx: int,
    horizons: Sequence[int],
) -> dict[str, Any]:
    """Forward returns (compounded daily returns of sessions AFTER signal) + drawdown."""
    max_h = max(horizons) if horizons else 0
    forward = series[signal_idx + 1 : signal_idx + 1 + max_h]
    sessions_available = len(forward)

    cum = 1.0
    cum_by_step: list[float] = []
    peak = 1.0
    max_dd = 0.0
    for r in forward:
        v = _safe_float(r.get("return_1d_pct"))
        if math.isnan(v):
            cum_by_step.append(float("nan"))
            continue
        cum *= 1.0 + v / 100.0
        cum_by_step.append(cum)
        peak = max(peak, cum)
        dd = cum / peak - 1.0
        max_dd = min(max_dd, dd)

    out: dict[str, Any] = {
        "signal_date": str(series[signal_idx].get("trade_date") or "")[:10],
        "action_signal": str(series[signal_idx].get("action_signal") or "") or None,
        "sessions_available": sessions_available,
    }
    for h in horizons:
        if sessions_available >= h and not math.isnan(cum_by_step[h - 1]):
            out[f"forward_{h}d_pct"] = round((cum_by_step[h - 1] - 1.0) * 100.0, 4)
        else:
            out[f"forward_{h}d_pct"] = float("nan")
    out[f"max_drawdown_{max_h}d_pct"] = round(max_dd * 100.0, 4) if sessions_available else float("nan")
    return out


def index_per_observation(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map ``(SYMBOL, signal_date)`` -> observation metrics for feedback-loop persistence."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for obs in result.get("per_observation") or []:
        if not isinstance(obs, dict):
            continue
        sym = str(obs.get("symbol") or "").strip().upper()
        sd = str(obs.get("signal_date") or "").strip()[:10]
        if sym and sd:
            out[(sym, sd)] = obs
    return out


def build_tape_forward_outcomes_patch(
    obs: dict[str, Any],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    computed_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``tape_extras['forward_outcomes']`` patch from one ``evaluate()`` observation."""
    hs = tuple(sorted({int(h) for h in horizons if int(h) > 0})) or DEFAULT_HORIZONS
    max_h = max(hs)
    patch: dict[str, Any] = {
        "sessions_available": obs.get("sessions_available"),
        "horizons": list(hs),
        "computed_at": computed_at or datetime.now().isoformat(timespec="seconds"),
    }
    for h in hs:
        patch[f"forward_{h}d_pct"] = obs.get(f"forward_{h}d_pct")
    patch[f"max_drawdown_{max_h}d_pct"] = obs.get(f"max_drawdown_{max_h}d_pct")
    return patch


def compute_forward_outcomes_for_rows(
    rows: Sequence[dict[str, Any]],
    *,
    start: str | date | None,
    end: str | date | None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Evaluate forward outcomes from pre-fetched feature rows (no Supabase read)."""
    result = evaluate(rows=rows, start=start, end=end, horizons=horizons)
    return index_per_observation(result)


def _aggregate(observations: Sequence[dict[str, Any]], horizons: Sequence[int]) -> dict[str, Any]:
    max_h = max(horizons) if horizons else 0
    agg: dict[str, Any] = {"observations": len(observations)}
    for h in horizons:
        vals = [
            o[f"forward_{h}d_pct"]
            for o in observations
            if not math.isnan(_safe_float(o.get(f"forward_{h}d_pct")))
        ]
        n = len(vals)
        wins = sum(1 for v in vals if v > 0.0)
        agg[f"horizon_{h}d"] = {
            "coverage": n,
            "win_rate_pct": round(100.0 * wins / n, 2) if n else None,
            "avg_return_pct": round(sum(vals) / n, 4) if n else None,
        }
    dd_key = f"max_drawdown_{max_h}d_pct"
    dds = [
        _safe_float(o.get(dd_key))
        for o in observations
        if not math.isnan(_safe_float(o.get(dd_key)))
    ]
    agg[dd_key] = {
        "coverage": len(dds),
        "avg_pct": round(sum(dds) / len(dds), 4) if dds else None,
        "worst_pct": round(min(dds), 4) if dds else None,
    }
    return agg


def evaluate(
    symbols: Sequence[str] | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    *,
    sectors: Sequence[str] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    exchange: str | None = None,
    buffer_days: int | None = None,
    cfg: Any = None,
    rows: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate realized forward returns for a symbol/sector set over a signal-date range.

    Args:
        symbols: explicit symbol set (case-insensitive). Either this or ``sectors``.
        start, end: inclusive signal-date range (ISO ``YYYY-MM-DD`` or date). Signal
            dates outside this range are ignored; forward sessions may extend past it.
        sectors: lowercase sector keys to expand into a symbol set (alternative to
            ``symbols``; both may be combined).
        horizons: forward session offsets to report (default +1 and +5).
        exchange: optional exchange filter (e.g. ``NSE``).
        buffer_days: calendar days fetched after ``end`` to expose forward sessions
            (defaults to ``max(horizons) * 3 + 7``).
        cfg: optional preloaded TitanConfig (avoids re-reading .env).
        rows: optional pre-fetched feature rows (skips the Supabase read; used in tests).

    Returns:
        dict with ``params``, ``per_symbol``, ``per_observation`` and ``cohort`` keys.
    """
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0})) or DEFAULT_HORIZONS
    start_iso = _to_iso(start)
    end_iso = _to_iso(end)
    if not start_iso or not end_iso:
        raise ValueError("start and end are required (ISO YYYY-MM-DD)")
    if not symbols and not sectors and rows is None:
        raise ValueError("provide symbols, sectors, or pre-fetched rows")

    max_h = max(horizons)
    buf = buffer_days if buffer_days is not None else max_h * 3 + 7
    end_dt = _parse_iso(end_iso)
    fetch_end_iso = (end_dt + timedelta(days=buf)).isoformat() if end_dt else end_iso

    if rows is None:
        rows = _fetch_feature_rows(
            symbols=symbols,
            sectors=sectors,
            start_iso=start_iso,
            end_iso=fetch_end_iso,
            exchange=exchange,
            cfg=cfg,
        )

    series_by_symbol = _series_by_symbol(rows)
    start_dt = _parse_iso(start_iso)

    per_symbol: dict[str, Any] = {}
    all_observations: list[dict[str, Any]] = []
    for symbol in sorted(series_by_symbol):
        series = series_by_symbol[symbol]
        sym_obs: list[dict[str, Any]] = []
        for idx, row in enumerate(series):
            td = _parse_iso(str(row.get("trade_date") or ""))
            if td is None:
                continue
            if start_dt and td < start_dt:
                continue
            if end_dt and td > end_dt:
                continue
            metrics = _forward_metrics_for_signal(series, idx, horizons)
            metrics["symbol"] = symbol
            sym_obs.append(metrics)
        if not sym_obs:
            continue
        all_observations.extend(sym_obs)
        symbol_summary = _aggregate(sym_obs, horizons)
        symbol_summary["signal_dates"] = [o["signal_date"] for o in sym_obs]
        per_symbol[symbol] = symbol_summary

    return {
        "params": {
            "symbols": sorted(series_by_symbol) if (symbols or rows is not None) else None,
            "sectors": [s.strip().lower() for s in sectors] if sectors else None,
            "start": start_iso,
            "end": end_iso,
            "horizons": list(horizons),
            "fetch_end": fetch_end_iso,
            "exchange": exchange,
        },
        "per_symbol": per_symbol,
        "per_observation": all_observations,
        "cohort": _aggregate(all_observations, horizons),
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "  -  "
    if isinstance(v, float):
        if math.isnan(v):
            return " nan "
        return f"{v:6.2f}"
    return str(v)


def format_report(result: dict[str, Any]) -> str:
    p = result.get("params", {})
    horizons = p.get("horizons", list(DEFAULT_HORIZONS))
    max_h = max(horizons) if horizons else 5
    dd_key = f"max_drawdown_{max_h}d_pct"

    lines: list[str] = []
    lines.append("=== Forward-return evaluation (sessions AFTER signal date) ===")
    lines.append(
        f"range={p.get('start')}..{p.get('end')} horizons={horizons} "
        f"sectors={p.get('sectors')} exchange={p.get('exchange') or 'any'}"
    )
    hdr_cols = "".join(f" win@{h}d  avg@{h}d " for h in horizons)
    lines.append(f"{'SYMBOL':<12}{'OBS':>4} {hdr_cols} {'avgDD':>7} {'worstDD':>8}")
    lines.append("-" * (12 + 5 + len(horizons) * 18 + 18))
    for symbol in sorted(result.get("per_symbol", {})):
        m = result["per_symbol"][symbol]
        cells = f"{symbol:<12}{m.get('observations', 0):>4} "
        for h in horizons:
            hh = m.get(f"horizon_{h}d", {})
            cells += f"{_fmt(hh.get('win_rate_pct'))}  {_fmt(hh.get('avg_return_pct'))} "
        dd = m.get(dd_key, {})
        cells += f" {_fmt(dd.get('avg_pct'))} {_fmt(dd.get('worst_pct'))}"
        lines.append(cells)

    c = result.get("cohort", {})
    lines.append("-" * (12 + 5 + len(horizons) * 18 + 18))
    cohort_cells = f"{'COHORT':<12}{c.get('observations', 0):>4} "
    for h in horizons:
        hh = c.get(f"horizon_{h}d", {})
        cohort_cells += f"{_fmt(hh.get('win_rate_pct'))}  {_fmt(hh.get('avg_return_pct'))} "
    dd = c.get(dd_key, {})
    cohort_cells += f" {_fmt(dd.get('avg_pct'))} {_fmt(dd.get('worst_pct'))}"
    lines.append(cohort_cells)
    lines.append(
        "win@Nd = % of signal dates with positive +N-session forward return; "
        "DD = post-signal max drawdown over the +{}-session window.".format(max_h)
    )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Forward-return evaluation harness (read-only).")
    ap.add_argument("--symbols", default="", help="comma-separated symbol set")
    ap.add_argument("--sectors", default="", help="comma-separated sector keys (lowercase)")
    ap.add_argument("--preset", choices=["twelve"], help="convenience symbol preset")
    ap.add_argument("--start", required=True, help="signal-date range start (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="signal-date range end (YYYY-MM-DD)")
    ap.add_argument("--horizons", default="1,5", help="forward sessions, e.g. 1,5")
    ap.add_argument("--exchange", default="", help="optional exchange filter, e.g. NSE")
    ap.add_argument("--buffer-days", type=int, default=None, help="calendar days fetched past --end")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    symbols: list[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.preset == "twelve":
        symbols = list(TWELVE_STOCKS)
    sectors = [s.strip() for s in args.sectors.split(",") if s.strip()]
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())

    result = evaluate(
        symbols=symbols or None,
        start=args.start,
        end=args.end,
        sectors=sectors or None,
        horizons=horizons or DEFAULT_HORIZONS,
        exchange=args.exchange or None,
        buffer_days=args.buffer_days,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
