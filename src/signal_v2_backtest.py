"""Offline A/B harness for legacy vs signal_v2 (spec section 8).

Pure metric helpers live here; ``scripts/signal_v2_backtest.py`` is the CLI entry.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterator, Sequence

from action_signals import (
    _derive_action_signal_legacy,
    derive_action_signal,
    normalize_action_signal,
)
from signal_v2 import evaluate_signal_v2

# Severity ordering (aligned with signal_v2._SEVERITY).
_LABEL_SEVERITY: dict[str, int] = {
    "buy": 0,
    "accumulate": 1,
    "hold": 2,
    "trim": 3,
    "exit-risk": 4,
}

_BULLISH = frozenset({"buy", "accumulate", "hold"})
_DEFENSIVE = frozenset({"trim", "exit-risk"})

# Per-layer ablation keys (legacy env names; layers are always on in production).
LAYER_FLAGS: tuple[tuple[str, str], ...] = (
    ("layer_a", "TITAN_SIGNAL_V2_LAYER_A"),
    ("layer_b", "TITAN_SIGNAL_V2_LAYER_B"),
    ("layer_c", "TITAN_SIGNAL_V2_LAYER_C"),
    ("layer_d", "TITAN_SIGNAL_V2_LAYER_D"),
    ("layer_e", "TITAN_SIGNAL_V2_LAYER_E"),
)

_TAPE_AUDIT_KEYS = (
    "return_5d_pct",
    "return_10d_pct",
    "return_20d_pct",
    "rel_return_5d_vs_nifty_pct",
    "rel_return_10d_vs_nifty_pct",
    "rel_return_20d_vs_nifty_pct",
    "next_day_score",
    "next_week_score",
    "sell_signal",
    "cmf_20",
    "adx_14",
    "rsi_14",
    "obv_slope_20",
    "ema200_stretch_atr",
    "gap_down_proxy",
    "sector_pctile_cmf_20",
    "sector_pctile_adx_14",
    "sector_pctile_ema200_stretch",
    "sector_pctile_effective_intent",
    "sector_pctile_return_5d_pct",
    "fundamental_status",
    "trap_exit_proxy",
    "high_volume_down_day_proxy",
    "panic_absorption_proxy",
    "liquidity_thin_proxy",
    "extreme_price_move_proxy",
    "atr_penalty_input",
    "divergence_bear_proxy",
    "divergence_bull_proxy",
    "pullback_quality_proxy",
    "stale_flow_obv_proxy",
)


def _safe_float(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def forward_return_eval_enabled() -> bool:
    """When set, metrics score FORWARD (+1 session) returns instead of same-day."""
    return os.environ.get("TITAN_FORWARD_RETURN_EVAL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _metric_return(row: dict[str, Any]) -> Any:
    """Return used for hit-rate / drawdown metrics.

    Default is the stored same-day ``return_1d_pct`` (trailing). When
    ``TITAN_FORWARD_RETURN_EVAL`` is on and a forward field is present on the row,
    the forward (+1 session) return is used instead so the scoreboard is non-circular.
    """
    if forward_return_eval_enabled():
        fwd = row.get("forward_return_1d_pct")
        if not math.isnan(_safe_float(fwd)):
            return fwd
    return row.get("return_1d_pct")


def return_direction(ret_pct: Any) -> str:
    """Mirror analysis_store._return_direction (0.3% dead band)."""
    v = _safe_float(ret_pct)
    if math.isnan(v):
        return "unknown"
    if v >= 0.3:
        return "up"
    if v <= -0.3:
        return "down"
    return "neutral"


def direction_hit(predicted: str, realized: str) -> bool | None:
    """Mirror analysis_store._direction_hit."""
    if predicted == "unknown" or realized == "unknown":
        return None
    if predicted == "neutral":
        return realized == "neutral"
    return predicted == realized


def signal_predicted_direction(label: str) -> str:
    """Map action label to directional bucket for hit-rate (accumulate -> up)."""
    key = normalize_action_signal(label)
    if key in ("buy", "accumulate"):
        return "up"
    if key in ("trim", "exit-risk"):
        return "down"
    return "neutral"


def label_severity(label: str) -> int:
    return _LABEL_SEVERITY.get(normalize_action_signal(label), 2)


def is_defensive_escalation(legacy_label: str, v2_label: str) -> bool:
    """Legacy was bullish-leaning; v2 is strictly more defensive."""
    leg = normalize_action_signal(legacy_label)
    v2 = normalize_action_signal(v2_label)
    return leg in _BULLISH and label_severity(v2) > label_severity(leg)


def is_false_exit_rescue(legacy_label: str, v2_label: str) -> bool:
    """Legacy trim/exit-risk; v2 relaxed to hold/accumulate/buy (D-3 style rescue)."""
    leg = normalize_action_signal(legacy_label)
    v2 = normalize_action_signal(v2_label)
    return leg in _DEFENSIVE and label_severity(v2) < label_severity(leg)


def drawdown_saved_pct(return_1d_pct: Any) -> float | None:
    """Next-day return avoided when flipping to a more defensive label (-r if r < 0)."""
    r = _safe_float(return_1d_pct)
    if math.isnan(r):
        return None
    return -r


def false_exit_forgone_pct(return_1d_pct: Any) -> float | None:
    """Opportunity cost when v2 stays long vs legacy trim/exit (positive r = forgone upside)."""
    r = _safe_float(return_1d_pct)
    if math.isnan(r):
        return None
    return r


def flip_rate(labels: Sequence[str]) -> float | None:
    if len(labels) < 2:
        return None
    changes = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    return round(changes / (len(labels) - 1), 4)


def _parse_tape_extras(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("tape_extras")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def feature_row_to_audit(row: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild an audit dict from a symbol_daily_features row (+ tape_extras)."""
    symbol = str(row.get("symbol") or "").strip()
    if not symbol:
        return None
    tape = _parse_tape_extras(row)
    nested = tape.get("backtest_audit")
    audit: dict[str, Any] = dict(nested) if isinstance(nested, dict) else {}
    audit.setdefault("symbol", symbol)
    audit.setdefault("exchange", row.get("exchange"))
    audit.setdefault("trade_date", row.get("trade_date"))
    for key in (
        "z_score",
        "ema_200_distance_pct",
        "atr_14_pct",
        "return_1d_pct",
        "intent_score",
        "effective_intent_score",
        "next_day_score",
        "next_week_score",
        "volume_participation_ratio",
        "absorption_ratio",
    ):
        if key in row and row[key] is not None:
            audit[key] = row[key]
    eff = audit.get("effective_intent_score", audit.get("intent_score"))
    if eff is not None and "effective_intent_score" not in audit:
        audit["effective_intent_score"] = eff
    for key in _TAPE_AUDIT_KEYS:
        if key in tape and tape[key] is not None:
            audit[key] = tape[key]
    if audit.get("next_week_score") is None and tape.get("next_week_score") is not None:
        audit["next_week_score"] = tape["next_week_score"]
    # Derive ATR-normalized EMA200 stretch from stored columns when it was not persisted
    # in tape_extras, so the over-extension (C-8) / ADX-regime (D) layers do not silently
    # run at zero on historical rows that predate the tape_extras risk-gate persistence.
    if _safe_float(audit.get("ema200_stretch_atr")) != _safe_float(audit.get("ema200_stretch_atr")):
        ema_dist = _safe_float(audit.get("ema_200_distance_pct"))
        atr_pct = _safe_float(audit.get("atr_14_pct"))
        if not math.isnan(ema_dist) and not math.isnan(atr_pct) and atr_pct != 0.0:
            audit["ema200_stretch_atr"] = round(ema_dist / atr_pct, 4)
    return audit


def audit_has_signal_inputs(audit: dict[str, Any]) -> bool:
    """Minimum fields to recompute labels (not merely compare stored action_signal)."""
    nw = _safe_float(audit.get("next_week_score"))
    eff = _safe_float(audit.get("effective_intent_score", audit.get("intent_score")))
    return not (math.isnan(nw) and math.isnan(eff))


@contextmanager
def signal_env(
    *,
    use_v2: bool,
    layer_overrides: dict[str, str] | None = None,
    accumulate: bool = False,
) -> Iterator[None]:
    """No-op context: v2 and accumulate are always on in production."""
    del use_v2, layer_overrides, accumulate
    yield


def recompute_label(
    audit: dict[str, Any],
    *,
    use_v2: bool,
    prior_label: str | None = None,
    layer_overrides: dict[str, str] | None = None,
    accumulate: bool = False,
) -> str:
    del layer_overrides, accumulate
    payload = dict(audit)
    for k in ("signal_engine_version", "signal_confidence", "signal_reason_trace", "action_signal", "sell_signal"):
        payload.pop(k, None)
    if prior_label:
        payload["prev_action_signal"] = prior_label
    if use_v2:
        label, _risk, _reasons = evaluate_signal_v2(payload)
    else:
        label, _risk, _reasons = _derive_action_signal_legacy(payload)
    return normalize_action_signal(label)


def sort_feature_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(r: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(r.get("symbol") or ""),
            str(r.get("exchange") or ""),
            str(r.get("trade_date") or ""),
        )

    return sorted(rows, key=_key)


def group_rows_by_symbol(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        ex = str(row.get("exchange") or "").strip().upper() or "NSE"
        if sym:
            out[(sym, ex)].append(row)
    for key in out:
        out[key] = sort_feature_rows(out[key])
    return dict(out)


def _attach_replay_memory(
    audit: dict[str, Any],
    prior_rows: list[dict[str, Any]],
) -> None:
    """Replay prior-session streaks and indicator trajectory from ordered feature rows."""
    if not prior_rows:
        return
    try:
        from signal_v2 import compute_indicator_trajectory, compute_prior_session_streaks
    except ImportError:
        return
    newest_first = list(reversed(prior_rows))
    streaks = compute_prior_session_streaks(newest_first)
    audit["prior_constructive_streak"] = streaks["prior_constructive_streak"]
    audit["prior_fail_streak"] = streaks["prior_fail_streak"]
    audit["indicator_trajectory"] = compute_indicator_trajectory(
        newest_first,
        current_audit=audit,
    )
    if len(newest_first) >= 1:
        prior_row = newest_first[0]
        prior = str(prior_row.get("action_signal") or "").strip().lower()
        if prior:
            audit["prev_action_signal"] = prior
        prior_tape = _parse_tape_extras(prior_row)
        prev_rel = prior_tape.get("rel_return_5d_vs_nifty_pct")
        if prev_rel is not None and _safe_float(prev_rel) == _safe_float(prev_rel):
            audit.setdefault("prev_rel_return_5d_vs_nifty_pct", prev_rel)
    if len(newest_first) >= 2:
        prev_prev = str(newest_first[1].get("action_signal") or "").strip().lower()
        if prev_prev:
            audit["prev_prev_action_signal"] = prev_prev


def walk_labels(
    rows: Sequence[dict[str, Any]],
    *,
    use_v2: bool,
    layer_overrides: dict[str, str] | None = None,
    accumulate: bool = False,
) -> list[tuple[dict[str, Any], str | None]]:
    """Return (row, label) pairs; label None when audit inputs insufficient."""
    prior: str | None = None
    out: list[tuple[dict[str, Any], str | None]] = []
    for idx, row in enumerate(rows):
        audit = feature_row_to_audit(row)
        if audit is None or not audit_has_signal_inputs(audit):
            out.append((row, None))
            continue
        _attach_replay_memory(audit, list(rows[:idx]))
        label = recompute_label(
            audit,
            use_v2=use_v2,
            prior_label=prior,
            layer_overrides=layer_overrides,
            accumulate=accumulate,
        )
        prior = label
        out.append((row, label))
    return out


@dataclass
class AbMetrics:
    """Aggregated spec section-8 metrics for one label stream vs a reference."""

    name: str
    row_count: int = 0
    recompute_skipped: int = 0
    defensive_escalation_events: int = 0
    drawdown_saved_sum: float = 0.0
    drawdown_saved_n: int = 0
    false_exit_rescue_events: int = 0
    false_exit_forgone_sum: float = 0.0
    false_exit_forgone_n: int = 0
    direction_hit: int = 0
    direction_total: int = 0
    flip_rate_mean: float | None = None
    flip_rate_vs_reference_delta: float | None = None
    label_counts: dict[str, int] = field(default_factory=dict)
    low_confidence_hit: int = 0
    low_confidence_total: int = 0
    high_confidence_hit: int = 0
    high_confidence_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        def _mean(num: float, den: int) -> float | None:
            return round(num / den, 4) if den else None

        return {
            "name": self.name,
            "row_count": self.row_count,
            "recompute_skipped": self.recompute_skipped,
            "defensive_escalation_events": self.defensive_escalation_events,
            "drawdown_saved_mean_pct": _mean(self.drawdown_saved_sum, self.drawdown_saved_n),
            "false_exit_rescue_events": self.false_exit_rescue_events,
            "false_exit_forgone_mean_pct": _mean(self.false_exit_forgone_sum, self.false_exit_forgone_n),
            "direction_hit_rate": (
                round(self.direction_hit / self.direction_total, 4) if self.direction_total else None
            ),
            "flip_rate_mean": self.flip_rate_mean,
            "flip_rate_vs_reference_delta": self.flip_rate_vs_reference_delta,
            "label_distribution": dict(sorted(self.label_counts.items())),
            "confidence_calibration": {
                "low_confidence_hit_rate": _mean(float(self.low_confidence_hit), self.low_confidence_total),
                "high_confidence_hit_rate": _mean(float(self.high_confidence_hit), self.high_confidence_total),
            },
        }


def accumulate_pair_metrics(
    metrics: AbMetrics,
    *,
    row: dict[str, Any],
    reference_label: str,
    candidate_label: str,
    confidence: float | None = None,
) -> None:
    metrics.row_count += 1
    ref = normalize_action_signal(reference_label)
    cand = normalize_action_signal(candidate_label)
    metrics.label_counts[cand] = metrics.label_counts.get(cand, 0) + 1

    ret = _metric_return(row)
    if is_defensive_escalation(ref, cand):
        metrics.defensive_escalation_events += 1
        saved = drawdown_saved_pct(ret)
        if saved is not None:
            metrics.drawdown_saved_sum += saved
            metrics.drawdown_saved_n += 1
    if is_false_exit_rescue(ref, cand):
        metrics.false_exit_rescue_events += 1
        forgone = false_exit_forgone_pct(ret)
        if forgone is not None:
            metrics.false_exit_forgone_sum += forgone
            metrics.false_exit_forgone_n += 1

    pred = signal_predicted_direction(cand)
    realized = return_direction(ret)
    hit = direction_hit(pred, realized)
    if hit is not None:
        metrics.direction_total += 1
        metrics.direction_hit += int(hit)
        if confidence is not None and not math.isnan(confidence):
            if confidence < 0.55:
                metrics.low_confidence_total += 1
                metrics.low_confidence_hit += int(hit)
            elif confidence >= 0.75:
                metrics.high_confidence_total += 1
                metrics.high_confidence_hit += int(hit)


def compare_label_streams(
    rows: Sequence[dict[str, Any]],
    *,
    reference_labels: Sequence[str | None],
    candidate_labels: Sequence[str | None],
    name: str,
    reference_flip_rate: float | None = None,
    confidences: Sequence[float | None] | None = None,
) -> AbMetrics:
    metrics = AbMetrics(name=name)
    per_symbol_labels: list[str] = []
    flip_rates: list[float] = []

    grouped = group_rows_by_symbol(rows)
    ref_by_idx = list(reference_labels)
    cand_by_idx = list(candidate_labels)

    if len(ref_by_idx) != len(rows) or len(cand_by_idx) != len(rows):
        raise ValueError("label sequences must match rows length")

    idx = 0
    for (_sym, _ex), sym_rows in sorted(grouped.items()):
        sym_cand: list[str] = []
        for row in sym_rows:
            ref = ref_by_idx[idx]
            cand = cand_by_idx[idx]
            conf = confidences[idx] if confidences and idx < len(confidences) else None
            idx += 1
            if ref is None or cand is None:
                metrics.recompute_skipped += 1
                continue
            accumulate_pair_metrics(
                metrics,
                row=row,
                reference_label=ref,
                candidate_label=cand,
                confidence=conf,
            )
            sym_cand.append(cand)
        fr = flip_rate(sym_cand)
        if fr is not None:
            flip_rates.append(fr)

    if flip_rates:
        metrics.flip_rate_mean = round(sum(flip_rates) / len(flip_rates), 4)
    if reference_flip_rate is not None and metrics.flip_rate_mean is not None:
        metrics.flip_rate_vs_reference_delta = round(
            metrics.flip_rate_mean - reference_flip_rate, 4
        )
    return metrics


def _flatten_label_walks(
    rows_sorted: Sequence[dict[str, Any]],
    *,
    use_v2: bool,
    layer_overrides: dict[str, str] | None = None,
    accumulate: bool = False,
    capture_v2_confidence: bool = False,
) -> tuple[list[str | None], list[float | None]]:
    labels: list[str | None] = []
    confidences: list[float | None] = []
    for (_sym, _ex), sym_rows in sorted(group_rows_by_symbol(rows_sorted).items()):
        for row, lab in walk_labels(
            sym_rows,
            use_v2=use_v2,
            layer_overrides=layer_overrides,
            accumulate=accumulate,
        ):
            labels.append(lab)
            conf: float | None = None
            if capture_v2_confidence and lab is not None:
                audit = feature_row_to_audit(row)
                if audit is not None:
                    payload = dict(audit)
                    with signal_env(
                        use_v2=True,
                        layer_overrides=layer_overrides,
                        accumulate=accumulate,
                    ):
                        derive_action_signal(payload)
                    cf = _safe_float(payload.get("signal_confidence"))
                    if not math.isnan(cf):
                        conf = cf
            confidences.append(conf)
    return labels, confidences


def _mean_flip_rate(
    rows_sorted: Sequence[dict[str, Any]],
    *,
    use_v2: bool,
    layer_overrides: dict[str, str] | None = None,
    accumulate: bool = False,
) -> float | None:
    frs: list[float] = []
    for (_sym, _ex), sym_rows in sorted(group_rows_by_symbol(rows_sorted).items()):
        labs = [
            lab
            for _r, lab in walk_labels(
                sym_rows,
                use_v2=use_v2,
                layer_overrides=layer_overrides,
                accumulate=accumulate,
            )
            if lab
        ]
        fr = flip_rate(labs)
        if fr is not None:
            frs.append(fr)
    return round(sum(frs) / len(frs), 4) if frs else None


def run_legacy_vs_v2_ab(
    rows: Sequence[dict[str, Any]],
    *,
    accumulate: bool = False,
    flip_guardrail_extra: float = 0.05,
) -> dict[str, Any]:
    """Full legacy vs v2 comparison plus per-layer ablations (master on)."""
    rows_sorted = sort_feature_rows(rows)
    legacy_labels, _ = _flatten_label_walks(rows_sorted, use_v2=False, accumulate=accumulate)
    v2_labels, v2_confidences = _flatten_label_walks(
        rows_sorted,
        use_v2=True,
        accumulate=accumulate,
        capture_v2_confidence=True,
    )
    legacy_fr = _mean_flip_rate(rows_sorted, use_v2=False, accumulate=accumulate)

    v2_metrics = compare_label_streams(
        rows_sorted,
        reference_labels=legacy_labels,
        candidate_labels=v2_labels,
        name="v2_vs_legacy",
        reference_flip_rate=legacy_fr,
        confidences=v2_confidences,
    )

    layer_reports: list[dict[str, Any]] = []
    for short, flag in LAYER_FLAGS:
        off_overrides = {flag: "0"}
        on_overrides = {flag: "1"}
        off_labels, _ = _flatten_label_walks(
            rows_sorted,
            use_v2=True,
            layer_overrides=off_overrides,
            accumulate=accumulate,
        )
        on_labels, _ = _flatten_label_walks(
            rows_sorted,
            use_v2=True,
            layer_overrides=on_overrides,
            accumulate=accumulate,
        )
        layer_reports.append(
            {
                "flag": flag,
                "short": short,
                "layer_off": compare_label_streams(
                    rows_sorted,
                    reference_labels=on_labels,
                    candidate_labels=off_labels,
                    name=f"{short}_off_vs_on",
                ).to_dict(),
            }
        )

    guardrail_ok = True
    if legacy_fr is not None and v2_metrics.flip_rate_mean is not None:
        guardrail_ok = v2_metrics.flip_rate_mean <= legacy_fr + flip_guardrail_extra

    stored_match = 0
    stored_total = 0
    for row in rows_sorted:
        stored = row.get("action_signal")
        if stored is None:
            continue
        audit = feature_row_to_audit(row)
        if audit is None or not audit_has_signal_inputs(audit):
            continue
        stored_total += 1
        leg = recompute_label(audit, use_v2=False, accumulate=accumulate)
        if normalize_action_signal(stored) == leg:
            stored_match += 1

    return {
        "legacy_vs_v2": v2_metrics.to_dict(),
        "legacy_flip_rate_mean": legacy_fr,
        "flip_guardrail_extra": flip_guardrail_extra,
        "flip_guardrail_pass": guardrail_ok,
        "stored_legacy_recompute_match_rate": (
            round(stored_match / stored_total, 4) if stored_total else None
        ),
        "per_layer_ablation": layer_reports,
    }


# Built-in offline fixture (no Supabase credentials required).
BUILTIN_FIXTURE_ROWS: list[dict[str, Any]] = [
    {
        "trade_date": "2026-05-01",
        "symbol": "FIX1",
        "exchange": "NSE",
        "sector": "fixture",
        "action_signal": "buy",
        "return_1d_pct": -2.4,
        "z_score": 2.2,
        "ema_200_distance_pct": 6.0,
        "atr_14_pct": 2.5,
        "effective_intent_score": 68.0,
        "intent_score": 68.0,
        "next_week_score": 72.0,
        "next_day_score": 58.0,
        "tape_extras": {
            "return_5d_pct": 4.0,
            "cmf_20": -0.18,
            "adx_14": 22.0,
            "ema200_stretch_atr": 2.8,
        },
    },
    {
        "trade_date": "2026-05-02",
        "symbol": "FIX1",
        "exchange": "NSE",
        "sector": "fixture",
        "action_signal": "hold",
        "return_1d_pct": 0.5,
        "z_score": 1.0,
        "ema_200_distance_pct": 5.0,
        "atr_14_pct": 2.4,
        "effective_intent_score": 60.0,
        "next_week_score": 58.0,
        "tape_extras": {"cmf_20": 0.05, "adx_14": 20.0},
    },
    {
        "trade_date": "2026-05-01",
        "symbol": "FIX2",
        "exchange": "NSE",
        "sector": "fixture",
        "action_signal": "trim",
        "return_1d_pct": 1.2,
        "z_score": -0.5,
        "ema_200_distance_pct": -1.0,
        "atr_14_pct": 3.0,
        "effective_intent_score": 52.0,
        "next_week_score": 56.0,
        "tape_extras": {
            "cmf_20": 0.12,
            "adx_14": 18.0,
            "pullback_quality_proxy": True,
        },
    },
    {
        "trade_date": "2026-05-02",
        "symbol": "FIX2",
        "exchange": "NSE",
        "sector": "fixture",
        "action_signal": "hold",
        "return_1d_pct": -0.2,
        "z_score": 0.2,
        "ema_200_distance_pct": 0.5,
        "atr_14_pct": 2.8,
        "effective_intent_score": 54.0,
        "next_week_score": 57.0,
        "tape_extras": {"cmf_20": 0.08, "adx_14": 19.0},
    },
]


def load_csv_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = dict(raw)
            if "tape_extras" in row and isinstance(row["tape_extras"], str):
                try:
                    row["tape_extras"] = json.loads(row["tape_extras"])
                except json.JSONDecodeError:
                    row["tape_extras"] = {}
            for key in ("return_1d_pct", "z_score", "next_week_score", "effective_intent_score"):
                if key in row and row[key] != "":
                    row[key] = float(row[key])
            rows.append(row)
    return rows


def fetch_supabase_rows(
    *,
    sector: str,
    lookback_days: int = 45,
) -> list[dict[str, Any]]:
    """Load symbol_daily_features history from Supabase (requires Titan env/config)."""
    from config_loader import load_config
    from supabase import create_client

    cfg = load_config()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    start = (date.today() - timedelta(days=max(lookback_days, 5))).isoformat()
    select_cols = (
        "trade_date,symbol,exchange,sector,action_signal,return_1d_pct,"
        "z_score,ema_200_distance_pct,atr_14_pct,intent_score,effective_intent_score,"
        "next_day_score,next_week_score,tape_extras"
    )
    res = (
        client.table("symbol_daily_features")
        .select(select_cols)
        .eq("sector", sector.strip().lower())
        .gte("trade_date", start)
        .order("trade_date")
        .execute()
    )
    rows = list(getattr(res, "data", None) or [])
    if forward_return_eval_enabled():
        _attach_forward_returns(rows)
    return rows


def _attach_forward_returns(rows: Sequence[dict[str, Any]]) -> None:
    """Annotate each row in-place with ``forward_return_1d_pct`` (next session's return).

    Forward is derived from the per-symbol ``return_1d_pct`` series (the move realized
    on the FOLLOWING session); rows without a next session keep NaN and fall back to
    the same-day metric. Mutates rows; safe no-op when the series has gaps.
    """
    for sym_rows in group_rows_by_symbol(rows).values():
        for i, row in enumerate(sym_rows):
            nxt = sym_rows[i + 1] if i + 1 < len(sym_rows) else None
            row["forward_return_1d_pct"] = (
                nxt.get("return_1d_pct") if nxt is not None else float("nan")
            )


def format_report(report: dict[str, Any]) -> str:
    lines = ["=== signal_v2 A/B backtest (spec section 8) ==="]
    core = report.get("legacy_vs_v2") or {}
    lines.append(f"Rows evaluated: {core.get('row_count', 0)} (skipped recompute: {core.get('recompute_skipped', 0)})")
    lines.append(
        f"Drawdown avoided (defensive flips): events={core.get('defensive_escalation_events')} "
        f"mean_saved_pct={core.get('drawdown_saved_mean_pct')}"
    )
    lines.append(
        f"False-exit cost (rescue flips): events={core.get('false_exit_rescue_events')} "
        f"mean_forgone_pct={core.get('false_exit_forgone_mean_pct')}"
    )
    lines.append(f"Direction hit-rate: {core.get('direction_hit_rate')}")
    lines.append(
        f"Flip-rate mean={core.get('flip_rate_mean')} "
        f"legacy={report.get('legacy_flip_rate_mean')} "
        f"delta={core.get('flip_rate_vs_reference_delta')} "
        f"guardrail_pass={report.get('flip_guardrail_pass')}"
    )
    lines.append(f"v2 label distribution: {core.get('label_distribution')}")
    cal = core.get("confidence_calibration") or {}
    lines.append(
        f"Confidence calibration: low_hit={cal.get('low_confidence_hit_rate')} "
        f"high_hit={cal.get('high_confidence_hit_rate')}"
    )
    stored = report.get("stored_legacy_recompute_match_rate")
    if stored is not None:
        lines.append(f"Stored action_signal vs legacy recompute match: {stored}")
    for layer in report.get("per_layer_ablation") or []:
        off = layer.get("layer_off") or {}
        lines.append(
            f"  [{layer.get('short')}] {layer.get('flag')} off: "
            f"drawdown_saved={off.get('drawdown_saved_mean_pct')} "
            f"hit_rate={off.get('direction_hit_rate')} "
            f"flip_delta={off.get('flip_rate_vs_reference_delta')}"
        )
    return "\n".join(lines)
