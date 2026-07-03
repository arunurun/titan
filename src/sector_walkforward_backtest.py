"""Sector walk-forward backtest (profile-off baseline + production / molded arms).

Replaces AI-only hardcoding with ``sector_key``-aware symbol loading, profile
application, and mold-artifact resolution. AI modules delegate here.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

from action_signals import normalize_action_signal
from signal_v2 import evaluate_signal_v2
from signal_v2_backtest import (
    _attach_replay_memory,
    audit_has_signal_inputs,
    feature_row_to_audit,
    group_rows_by_symbol,
    sort_feature_rows,
)

ROOT = Path(__file__).resolve().parents[1]
ALLOWLISTS_DIR = ROOT / "data" / "sector_allowlists"
SECTORS_CSV_DIR = ROOT / "data" / "sectors"
MAX_GAP_DAYS = 4
_BUY_LABELS = frozenset({"buy", "accumulate"})
_BASELINE_PROFILE_SENTINEL = {"__baseline_arm__": True}

_FETCH_COLS = (
    "trade_date,symbol,exchange,sector,action_signal,return_1d_pct,"
    "z_score,ema_200_distance_pct,atr_14_pct,intent_score,effective_intent_score,"
    "next_day_score,next_week_score,volume_participation_ratio,absorption_ratio,"
    "tape_extras"
)
_FETCH_PAGE = 1000

COMPARISON_ARMS: tuple[str, ...] = (
    "baseline",
    "production",
    "3b",
    "3c",
    "tuned",
)

FUSION_COMPARISON_ARMS: tuple[str, ...] = (
    "fusion_off",
    "fusion_on",
)


@contextmanager
def _signal_env_override(env: dict[str, str] | None):
    if not env:
        yield
        return
    saved: dict[str, str | None] = {}
    try:
        for key, val in env.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = str(val)
        yield
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def _safe_float(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x


def _parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _to_iso(d: str | date | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    return s or None


def _normalize_sector_key(sector_key: str) -> str:
    from sector_registry import resolve_sector_key

    return resolve_sector_key(str(sector_key or "").strip())


def load_sector_symbols_from_allowlist(sector_key: str) -> list[str]:
    """Symbol list from ``data/sector_allowlists/<sector_key>.json``."""
    sec = _normalize_sector_key(sector_key)
    path = ALLOWLISTS_DIR / f"{sec}.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    out: list[str] = []
    for raw in data.get("symbols") or []:
        sym = str(raw or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_sector_symbols_from_csv(sector_key: str) -> list[str]:
    """Symbol list from ``data/sectors/<sector_key>.csv``."""
    sec = _normalize_sector_key(sector_key)
    path = SECTORS_CSV_DIR / f"{sec}.csv"
    if not path.is_file():
        return []
    seen: set[str] = set()
    out: list[str] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = str(row.get("symbol") or "").strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def load_sector_backtest_symbols(sector_key: str) -> list[str]:
    """Load sector universe: registry (Supabase/CSV) → allowlist JSON → sector CSV."""
    sec = _normalize_sector_key(sector_key)
    try:
        from sector_registry import load_sector_symbols

        syms = load_sector_symbols(sec)
        if syms:
            return syms
    except Exception as exc:  # noqa: BLE001
        print(f"[SectorBacktest] registry load failed for {sec!r}: {exc}")
    allow = load_sector_symbols_from_allowlist(sec)
    if allow:
        return allow
    csv_syms = load_sector_symbols_from_csv(sec)
    if csv_syms:
        return csv_syms
    raise FileNotFoundError(
        f"No symbols for sector {sec!r}: checked registry, {ALLOWLISTS_DIR / f'{sec}.json'}, "
        f"and {SECTORS_CSV_DIR / f'{sec}.csv'}"
    )


def sector_has_signal_profile(sector_key: str) -> bool:
    from sector_priority import sector_signal_profile_for

    return bool(sector_signal_profile_for(_normalize_sector_key(sector_key)))


def fetch_sector_rows(
    supabase: Any,
    sector_key: str,
    start: str | date,
    end: str | date,
    buffer_days: int,
    *,
    symbols: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Paginated read of ``symbol_daily_features`` for a sector universe."""
    start_iso = _to_iso(start)
    end_iso = _to_iso(end)
    if not start_iso or not end_iso:
        raise ValueError("start and end are required (ISO YYYY-MM-DD)")
    end_dt = _parse_iso(end_iso)
    fetch_end = (end_dt + timedelta(days=max(buffer_days, 0))).isoformat() if end_dt else end_iso
    sym_list = [
        s.strip().upper()
        for s in (symbols or load_sector_backtest_symbols(sector_key))
        if s.strip()
    ]

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        q = (
            supabase.table("symbol_daily_features")
            .select(_FETCH_COLS)
            .gte("trade_date", start_iso)
            .lte("trade_date", fetch_end)
            .in_("symbol", sym_list)
            .order("trade_date")
            .range(offset, offset + _FETCH_PAGE - 1)
        )
        batch = list(getattr(q.execute(), "data", None) or [])
        rows.extend(batch)
        if len(batch) < _FETCH_PAGE:
            break
        offset += _FETCH_PAGE
    return rows


def _parse_tape(row: dict[str, Any]) -> dict[str, Any]:
    tape = row.get("tape_extras")
    if isinstance(tape, dict):
        return tape
    if isinstance(tape, str) and tape.strip():
        try:
            parsed = json.loads(tape)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_vitals(row: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    tape = _parse_tape(row)
    keys = (
        "effective_intent_score",
        "next_week_score",
        "cmf_20",
        "volume_participation_ratio",
        "ema200_stretch_atr",
        "return_5d_pct",
        "sector_pctile_effective_intent",
    )
    out: dict[str, Any] = {}
    for key in keys:
        val = audit.get(key)
        if val is None:
            val = row.get(key)
        if val is None:
            val = tape.get(key)
        out[key] = val
    if out.get("effective_intent_score") is None:
        out["effective_intent_score"] = row.get("intent_score")
    return out


def _forward_metrics_gap_guarded(
    series: list[dict[str, Any]],
    signal_idx: int,
    horizons: Sequence[int],
) -> dict[str, Any]:
    """Compound forward returns after signal date; stop at calendar gaps > MAX_GAP_DAYS."""
    max_h = max(horizons) if horizons else 0
    dates = [_parse_iso(str(r.get("trade_date") or "")) for r in series]
    rets = [_safe_float(r.get("return_1d_pct")) for r in series]

    cum = 1.0
    cum_by_step: list[float] = []
    peak = 1.0
    max_dd = 0.0
    k = 0
    j = signal_idx + 1
    while j < len(series) and k < max_h:
        if dates[j] is None or dates[j - 1] is None:
            break
        if (dates[j] - dates[j - 1]).days > MAX_GAP_DAYS:
            break
        rj = rets[j]
        if math.isnan(rj):
            break
        cum *= 1.0 + rj / 100.0
        cum_by_step.append(cum)
        peak = max(peak, cum)
        max_dd = min(max_dd, cum / peak - 1.0)
        k += 1
        j += 1

    out: dict[str, Any] = {
        "signal_date": str(series[signal_idx].get("trade_date") or "")[:10],
        "sessions_available": k,
    }
    for h in horizons:
        if k >= h and len(cum_by_step) >= h and not math.isnan(cum_by_step[h - 1]):
            out[f"forward_{h}d_pct"] = round((cum_by_step[h - 1] - 1.0) * 100.0, 4)
        else:
            out[f"forward_{h}d_pct"] = float("nan")
    out[f"max_drawdown_{max_h}d_pct"] = round(max_dd * 100.0, 4) if k else float("nan")
    return out


def _aggregate_forward(observations: Sequence[dict[str, Any]], horizons: Sequence[int]) -> dict[str, Any]:
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


def _summarize_walkforward_result(
    result: dict[str, Any],
    *,
    horizon: int = 5,
    extra_horizons: Sequence[int] = (1, 10, 15),
) -> dict[str, Any]:
    """Compact buy-cohort summary for comparison reports (no external mold module)."""
    cohort = result.get("cohort") or {}
    buy_fwd = cohort.get("buy_forward") or {}
    horizons = tuple(sorted({int(h) for h in (horizon, *extra_horizons) if int(h) > 0}))
    out: dict[str, Any] = {
        "buy_signals": cohort.get("buy_signals"),
        "observations": cohort.get("observations"),
    }
    for h in horizons:
        block = buy_fwd.get(f"horizon_{h}d") or {}
        out[f"buy_avg_fwd_{h}d_pct"] = block.get("avg_return_pct")
        if h == horizon:
            vals = [
                o.get(f"forward_{h}d_pct")
                for o in result.get("per_observation") or []
                if str(o.get("label") or "").lower() in _BUY_LABELS
                and not math.isnan(_safe_float(o.get(f"forward_{h}d_pct")))
            ]
            declines = sum(1 for v in vals if float(v) < 0.0)
            out[f"buy_decline_rate_{h}d"] = round(declines / len(vals), 4) if vals else None
    return out


def _fusion_enabled_from_env(env: dict[str, str] | None = None) -> bool:
    raw = (env or {}).get("TITAN_FUSION_ENABLED")
    if raw is None:
        raw = os.environ.get("TITAN_FUSION_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _replay_fusion_on_audit(
    audit: dict[str, Any],
    *,
    env_override: dict[str, str] | None = None,
) -> None:
    """Populate factor_scores and stamp titan_fusion for walk-forward replay."""
    merged = dict(env_override or {})
    if not _fusion_enabled_from_env(merged):
        audit.pop("titan_fusion", None)
        audit.pop("titan_score", None)
        return
    try:
        from sector_audit import _populate_factor_scores
        from titan_fusion import apply_fusion_to_audit

        _populate_factor_scores(audit)
        apply_fusion_to_audit(audit)
    except ImportError:
        pass


def _apply_profile_arm(
    audit: dict[str, Any],
    profile_arm: str,
    sector_key: str,
    *,
    profile_override: dict[str, float] | None = None,
) -> None:
    """Baseline skips auto-loaded sector_signal_profile; production uses defaults."""
    sec = _normalize_sector_key(sector_key)
    audit.setdefault("sector_key", sec)
    audit.setdefault("sector", sec)
    if profile_arm == "baseline":
        audit["sector_signal_profile"] = dict(_BASELINE_PROFILE_SENTINEL)
    elif profile_override:
        from sector_priority import sector_signal_profile_for

        base = sector_signal_profile_for(sec)
        audit["sector_signal_profile"] = {**base, **profile_override}
    else:
        audit.pop("sector_signal_profile", None)


def _evaluate_label(
    audit: dict[str, Any],
    prior_label: str | None,
    *,
    env_override: dict[str, str] | None = None,
) -> tuple[str, float | None]:
    payload = dict(audit)
    for k in ("signal_engine_version", "signal_confidence", "signal_reason_trace", "action_signal", "sell_signal"):
        payload.pop(k, None)
    if prior_label:
        payload["prev_action_signal"] = prior_label
    with _signal_env_override(env_override):
        label, _risk, _reasons = evaluate_signal_v2(payload)
    conf_raw = payload.get("signal_confidence")
    conf = _safe_float(conf_raw)
    confidence = None if math.isnan(conf) else conf
    return normalize_action_signal(label), confidence


def _recompute_predictive_scores(
    audit: dict[str, Any],
    weight_override: dict[str, float] | None = None,
    *,
    use_sector_predictive_weights: bool = True,
    sector_key: str = "",
) -> None:
    """Re-derive next_day/week scores from audit vitals using molded coefficients."""
    from sector_audit import (
        _predictive_scores,
        predictive_weight_override,
        sector_predictive_weights_enabled,
    )

    sec = _normalize_sector_key(sector_key or str(audit.get("sector_key") or ""))
    allow_sector_weights = use_sector_predictive_weights and sec == "ai"
    with sector_predictive_weights_enabled(allow_sector_weights):
        with predictive_weight_override(weight_override):
            next_day_score, next_week_score, prediction_breakdown = _predictive_scores(audit)
    audit["next_day_score"] = next_day_score
    audit["next_week_score"] = next_week_score
    audit["prediction_breakdown"] = prediction_breakdown


def data_coverage_summary(
    rows: Sequence[dict[str, Any]],
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Report symbol/date coverage for fetched feature rows."""
    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    sym_set = {s.strip().upper() for s in symbols if s.strip()}
    rows_in_window: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        td = _parse_iso(str(row.get("trade_date") or ""))
        if not sym or td is None or start_dt is None or end_dt is None:
            continue
        if start_dt <= td <= end_dt:
            rows_in_window[sym].add(td.isoformat())
    missing = sorted(sym_set - set(rows_in_window))
    sparse = sorted(sym for sym, dates in rows_in_window.items() if len(dates) < 5)
    return {
        "symbols_declared": len(sym_set),
        "symbols_with_rows": len(rows_in_window),
        "symbols_missing": missing,
        "symbols_sparse_lt5_sessions": sparse,
        "total_rows": len(rows),
    }


def run_walkforward(
    *,
    sector_key: str,
    start: str = "2026-06-01",
    end: str = "2026-06-30",
    horizons: Sequence[int] = (1, 5, 10, 15),
    profile_arm: str = "baseline",
    profile_override: dict[str, float] | None = None,
    weight_override: dict[str, float] | None = None,
    env_override: dict[str, str] | None = None,
    recompute_predictive_scores: bool = False,
    use_sector_predictive_weights: bool | None = None,
    rows: Sequence[dict[str, Any]] | None = None,
    supabase: Any | None = None,
    cfg: Any | None = None,
    buffer_days: int | None = None,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Walk-forward replay + forward-return scoring for a sector universe."""
    sec = _normalize_sector_key(sector_key)
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0}))
    if not horizons:
        raise ValueError("horizons must contain at least one positive integer")
    start_iso = _to_iso(start)
    end_iso = _to_iso(end)
    if not start_iso or not end_iso:
        raise ValueError("start and end are required (ISO YYYY-MM-DD)")
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    if use_sector_predictive_weights is None:
        use_sector_predictive_weights = False
    max_h = max(horizons)
    buf = buffer_days if buffer_days is not None else max_h * 3 + 7
    sym_list = list(symbols) if symbols is not None else load_sector_backtest_symbols(sec)

    if rows is None:
        if supabase is None:
            from config_loader import load_config
            from supabase import create_client

            cfg = cfg or load_config(require_breeze=False, require_gemini=False)
            supabase = create_client(cfg.supabase_url, cfg.supabase_key)
        rows = fetch_sector_rows(supabase, sec, start_iso, end_iso, buf, symbols=sym_list)

    rows_sorted = sort_feature_rows(rows)
    grouped = group_rows_by_symbol(rows_sorted)
    coverage = data_coverage_summary(rows_sorted, sym_list, start=start_iso, end=end_iso)

    all_observations: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}

    for (symbol, _exchange), sym_rows in sorted(grouped.items()):
        prior_label: str | None = None
        sym_obs: list[dict[str, Any]] = []
        for idx, row in enumerate(sym_rows):
            td = _parse_iso(str(row.get("trade_date") or ""))
            if td is None or start_dt is None or end_dt is None:
                continue
            if td < start_dt or td > end_dt:
                continue
            audit = feature_row_to_audit(row)
            if audit is None or not audit_has_signal_inputs(audit):
                continue
            _attach_replay_memory(audit, list(sym_rows[:idx]))
            _apply_profile_arm(audit, profile_arm, sec, profile_override=profile_override)
            if recompute_predictive_scores or weight_override:
                _recompute_predictive_scores(
                    audit,
                    weight_override,
                    use_sector_predictive_weights=use_sector_predictive_weights and not weight_override,
                    sector_key=sec,
                )
            _replay_fusion_on_audit(audit, env_override=env_override)
            label, confidence = _evaluate_label(audit, prior_label, env_override=env_override)
            prior_label = label

            fwd = _forward_metrics_gap_guarded(sym_rows, idx, horizons)
            obs: dict[str, Any] = {
                "symbol": symbol,
                "signal_date": fwd["signal_date"],
                "label": label,
                "confidence": confidence,
                "sessions_available": fwd["sessions_available"],
                "vitals": _extract_vitals(row, audit),
            }
            for h in horizons:
                obs[f"forward_{h}d_pct"] = fwd.get(f"forward_{h}d_pct")
            obs[f"max_drawdown_{max_h}d_pct"] = fwd.get(f"max_drawdown_{max_h}d_pct")
            sym_obs.append(obs)
            all_observations.append(obs)

        if not sym_obs:
            continue
        sym_summary = _aggregate_forward(sym_obs, horizons)
        sym_summary["label_counts"] = dict(
            sorted(
                ((lab, sum(1 for o in sym_obs if o["label"] == lab)) for lab in {o["label"] for o in sym_obs}),
                key=lambda x: x[0],
            )
        )
        buy_obs = [o for o in sym_obs if o["label"] in _BUY_LABELS]
        sym_summary["buy_signals"] = len(buy_obs)
        sym_summary["buy_forward"] = _aggregate_forward(buy_obs, horizons) if buy_obs else {"observations": 0}
        per_symbol[symbol] = sym_summary

    buy_cohort = [o for o in all_observations if o["label"] in _BUY_LABELS]
    label_counts: dict[str, int] = defaultdict(int)
    for o in all_observations:
        label_counts[str(o.get("label") or "unknown")] += 1

    cohort: dict[str, Any] = _aggregate_forward(all_observations, horizons)
    cohort["label_counts"] = dict(sorted(label_counts.items()))
    cohort["buy_signals"] = len(buy_cohort)
    cohort["buy_forward"] = _aggregate_forward(buy_cohort, horizons) if buy_cohort else {"observations": 0}

    end_dt_fetch = end_dt + timedelta(days=buf) if end_dt else None
    return {
        "params": {
            "sector_key": sec,
            "symbols": sorted({str(r.get("symbol") or "").upper() for r in rows_sorted if r.get("symbol")}),
            "symbols_declared": sorted(sym_list),
            "has_sector_signal_profile": sector_has_signal_profile(sec),
            "start": start_iso,
            "end": end_iso,
            "horizons": list(horizons),
            "fetch_end": end_dt_fetch.isoformat() if end_dt_fetch else end_iso,
            "profile_arm": profile_arm,
            "profile_override": dict(profile_override) if profile_override else None,
            "weight_override": dict(weight_override) if weight_override else None,
            "env_override": dict(env_override) if env_override else None,
            "recompute_predictive_scores": recompute_predictive_scores or bool(weight_override),
            "use_sector_predictive_weights": use_sector_predictive_weights,
            "max_gap_days": MAX_GAP_DAYS,
        },
        "data_coverage": coverage,
        "per_observation": all_observations,
        "per_symbol": per_symbol,
        "cohort": cohort,
    }


def _arm_kwargs(
    arm: str,
    sector_key: str,
    *,
    report_dir: Path | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    from sector_tuned import load_best_3b_weights, load_best_3c_gates, sector_tuned_bundle

    sec = _normalize_sector_key(sector_key)
    out_dir = report_dir
    if arm == "baseline":
        return {"profile_arm": "baseline"}
    if arm == "production":
        return {"profile_arm": "production", "use_sector_predictive_weights": False}
    if arm == "3b":
        weights = load_best_3b_weights(sec, out_dir, start=start, end=end)
        return {
            "profile_arm": "production",
            "weight_override": weights or None,
            "recompute_predictive_scores": bool(weights),
            "use_sector_predictive_weights": False,
        }
    if arm == "3c":
        profile, env = load_best_3c_gates(sec, out_dir, start=start, end=end)
        return {
            "profile_arm": "production",
            "profile_override": profile or None,
            "env_override": env or None,
            "use_sector_predictive_weights": False,
        }
    if arm == "tuned":
        bundle = sector_tuned_bundle(sec, out_dir, start=start, end=end)
        return {
            "profile_arm": "production",
            "profile_override": bundle.get("profile_override"),
            "weight_override": bundle.get("weight_override"),
            "env_override": bundle.get("env_override"),
            "recompute_predictive_scores": bundle.get("recompute_predictive_scores", True),
            "use_sector_predictive_weights": False,
        }
    raise ValueError(f"unknown arm: {arm}")


def _fusion_arm_kwargs(arm: str) -> dict[str, Any]:
    """Fusion on/off arms — production profile with fusion env toggle."""
    if arm == "fusion_off":
        return {
            "profile_arm": "production",
            "use_sector_predictive_weights": False,
            "env_override": {"TITAN_FUSION_ENABLED": "0", "TITAN_FUSION_SIGV2_BLEND": "0"},
        }
    if arm == "fusion_on":
        return {
            "profile_arm": "production",
            "use_sector_predictive_weights": False,
            "env_override": {"TITAN_FUSION_ENABLED": "1"},
        }
    raise ValueError(f"unknown fusion arm: {arm}")


def run_fusion_arm_comparison(
    *,
    sector_key: str,
    start: str,
    end: str,
    horizons: Sequence[int] = (1, 5, 15),
    arms: Sequence[str] = FUSION_COMPARISON_ARMS,
    rows: Sequence[dict[str, Any]] | None = None,
    supabase: Any | None = None,
    cfg: Any | None = None,
    buffer_days: int | None = None,
) -> dict[str, Any]:
    """Compare fusion-disabled vs fusion-enabled on production profile."""
    sec = _normalize_sector_key(sector_key)
    shared: dict[str, Any] = {
        "sector_key": sec,
        "start": start,
        "end": end,
        "horizons": horizons,
        "rows": rows,
        "supabase": supabase,
        "cfg": cfg,
        "buffer_days": buffer_days,
    }
    arm_results: dict[str, Any] = {}
    for arm in arms:
        result = run_walkforward(**shared, **_fusion_arm_kwargs(arm))
        arm_results[arm] = {
            "params": result.get("params"),
            "data_coverage": result.get("data_coverage"),
            "summary": _summarize_walkforward_result(result, horizon=5, extra_horizons=(1, 10, 15)),
            "cohort": result.get("cohort"),
        }
    return {
        "params": {
            "sector_key": sec,
            "start": start,
            "end": end,
            "horizons": list(horizons),
            "arms": list(arms),
            "comparison": "fusion_on_vs_off",
            "has_sector_signal_profile": sector_has_signal_profile(sec),
        },
        "arms": arm_results,
    }


def run_arm_comparison(
    *,
    sector_key: str,
    start: str,
    end: str,
    horizons: Sequence[int] = (1, 5, 15),
    arms: Sequence[str] = COMPARISON_ARMS,
    rows: Sequence[dict[str, Any]] | None = None,
    supabase: Any | None = None,
    cfg: Any | None = None,
    buffer_days: int | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    sec = _normalize_sector_key(sector_key)
    shared: dict[str, Any] = {
        "sector_key": sec,
        "start": start,
        "end": end,
        "horizons": horizons,
        "rows": rows,
        "supabase": supabase,
        "cfg": cfg,
        "buffer_days": buffer_days,
    }
    arm_results: dict[str, Any] = {}
    for arm in arms:
        result = run_walkforward(
            **shared,
            **_arm_kwargs(arm, sec, report_dir=report_dir, start=start, end=end),
        )
        arm_results[arm] = {
            "params": result.get("params"),
            "data_coverage": result.get("data_coverage"),
            "summary": _summarize_walkforward_result(result, horizon=5, extra_horizons=(1, 10, 15)),
            "cohort": result.get("cohort"),
        }
    return {
        "params": {
            "sector_key": sec,
            "start": start,
            "end": end,
            "horizons": list(horizons),
            "arms": list(arms),
            "has_sector_signal_profile": sector_has_signal_profile(sec),
        },
        "arms": arm_results,
    }


def _fmt_cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isnan(v):
        return "nan"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def format_final_comparison_report(report: dict[str, Any]) -> str:
    p = report.get("params") or {}
    arms = report.get("arms") or {}
    sec = p.get("sector_key") or "sector"
    lines = [
        f"# {sec} sector backtest: Phase 3b + 3c comparison",
        "",
        f"Window: **{p.get('start')}** to **{p.get('end')}**",
        f"Sector signal profile: **{'yes' if p.get('has_sector_signal_profile') else 'no (production = default thresholds)'}**",
        "",
        "## Buy-rated forward returns by arm",
        "",
        "| Arm | Buy # | T+1d avg % | T+5d avg % | T+10d avg % | T+15d avg % | Decline T+5 | Decline T+15 | Flip rate |",
        "|-----|-------|------------|------------|-------------|-------------|-------------|--------------|-----------|",
    ]
    labels = {
        "baseline": "baseline (profile-off, default weights)",
        "production": "production (sector thresholds, default weights)",
        "3b": "3b only (best weights)",
        "3c": "3c only (best gates)",
        "tuned": "tuned (3b + 3c)",
    }
    for arm in p.get("arms") or COMPARISON_ARMS:
        block = arms.get(arm) or {}
        s = block.get("summary") or {}
        lines.append(
            f"| {labels.get(arm, arm)} | {s.get('buy_signals', '-')} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_1d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_5d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_10d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_15d_pct'))} | "
            f"{_fmt_cell(s.get('buy_decline_rate_5d'))} | "
            f"{_fmt_cell(s.get('buy_decline_rate_15d'))} | "
            f"{_fmt_cell(s.get('flip_rate_mean'))} |"
        )

    base = (arms.get("baseline") or {}).get("summary") or {}
    lines.extend(
        [
            "",
            "## Delta vs baseline (buy avg %)",
            "",
            "| Arm | dT+1 | dT+5 | dT+10 | dT+15 |",
            "|-----|------|------|-------|-------|",
        ]
    )
    for arm in p.get("arms") or COMPARISON_ARMS:
        if arm == "baseline":
            continue
        s = (arms.get(arm) or {}).get("summary") or {}
        deltas = []
        for h in (1, 5, 10, 15):
            bv = base.get(f"buy_avg_fwd_{h}d_pct")
            sv = s.get(f"buy_avg_fwd_{h}d_pct")
            if bv is not None and sv is not None:
                deltas.append(f"{float(sv) - float(bv):+.2f}")
            else:
                deltas.append("-")
        lines.append(f"| {labels.get(arm, arm)} | {' | '.join(deltas)} |")

    cov = (arms.get("baseline") or {}).get("data_coverage") or {}
    if cov:
        lines.extend(
            [
                "",
                "## Data coverage (baseline fetch)",
                "",
                f"- Symbols declared: {cov.get('symbols_declared')}",
                f"- Symbols with rows in window: {cov.get('symbols_with_rows')}",
                f"- Missing: {', '.join(cov.get('symbols_missing') or []) or 'none'}",
                f"- Sparse (<5 sessions): {', '.join(cov.get('symbols_sparse_lt5_sessions') or []) or 'none'}",
            ]
        )

    return "\n".join(lines) + "\n"


def format_fusion_comparison_report(report: dict[str, Any]) -> str:
    """Markdown table for fusion_on vs fusion_off walk-forward arms."""
    p = report.get("params") or {}
    arms = report.get("arms") or {}
    sec = p.get("sector_key") or "sector"
    labels = {
        "fusion_off": "fusion off (TITAN_FUSION_ENABLED=0)",
        "fusion_on": "fusion on (default production)",
    }
    lines = [
        f"# {sec} sector backtest: fusion comparison",
        "",
        f"Window: **{p.get('start')}** to **{p.get('end')}**",
        "Profile: **production** — arms differ only by fusion env.",
        "",
        "## Buy-rated forward returns",
        "",
        "| Arm | Buy # | T+1d avg % | T+5d avg % | T+10d avg % | T+15d avg % |",
        "|-----|-------|------------|------------|-------------|-------------|",
    ]
    for arm in p.get("arms") or FUSION_COMPARISON_ARMS:
        s = (arms.get(arm) or {}).get("summary") or {}
        lines.append(
            f"| {labels.get(arm, arm)} | {s.get('buy_signals', '-')} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_1d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_5d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_10d_pct'))} | "
            f"{_fmt_cell(s.get('buy_avg_fwd_15d_pct'))} |"
        )
    off = (arms.get("fusion_off") or {}).get("summary") or {}
    on = (arms.get("fusion_on") or {}).get("summary") or {}
    lines.extend(["", "## Delta (fusion_on − fusion_off, buy avg %)"])
    for h in (1, 5, 10, 15):
        ov = off.get(f"buy_avg_fwd_{h}d_pct")
        nv = on.get(f"buy_avg_fwd_{h}d_pct")
        if ov is not None and nv is not None:
            lines.append(f"- T+{h}d: {float(nv) - float(ov):+.2f}%")
    return "\n".join(lines) + "\n"


def write_final_comparison_artifact(
    report: dict[str, Any],
    output_dir: Path,
    *,
    filename: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    sec = (report.get("params") or {}).get("sector_key") or "sector"
    path = output_dir / (filename or f"comparison_{sec}_may_june_2026.md")
    path.write_text(format_final_comparison_report(report), encoding="utf-8")
    return path
