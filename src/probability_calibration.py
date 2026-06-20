"""Historical score → P(up 5d) calibration layer (always-on).

``TITAN_PROB_CALIB_MODE``: ``shadow`` (record only) or ``enforce`` (replace confidence).
"""

from __future__ import annotations

import math
import os
from typing import Any

_DEFAULT_BUCKETS: tuple[tuple[float, float, float], ...] = (
    (0.0, 45.0, 0.28),
    (45.0, 52.0, 0.32),
    (52.0, 58.0, 0.36),
    (58.0, 65.0, 0.42),
    (65.0, 72.0, 0.48),
    (72.0, 80.0, 0.55),
    (80.0, 100.0, 0.62),
)


def _calibration_mode() -> str:
    raw = os.environ.get("TITAN_PROB_CALIB_MODE", "enforce").strip().lower()
    return raw if raw in ("shadow", "enforce") else "enforce"


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def calibrate_probability(
    score: float,
    *,
    sector: str | None = None,
    buckets: tuple[tuple[float, float, float], ...] | None = None,
) -> float:
    s = _sf(score)
    if math.isnan(s):
        return float("nan")
    table = buckets or _DEFAULT_BUCKETS
    sector_adj = 0.0
    if sector:
        key = str(sector).strip().lower()
        if key in ("telecom", "defence"):
            sector_adj = 0.03
        elif key in ("pharma_healthcare",):
            sector_adj = -0.02
    for lo, hi, prob in table:
        if lo <= s < hi or (hi == table[-1][1] and s >= lo):
            return round(_clamp(prob + sector_adj, 0.0, 1.0), 4)
    return round(_clamp(table[-1][2] + sector_adj, 0.0, 1.0), 4)


class ProbabilityCalibrator:
    def __init__(
        self,
        *,
        buckets: tuple[tuple[float, float, float], ...] | None = None,
    ) -> None:
        self.buckets = buckets or _DEFAULT_BUCKETS

    def predict(self, score: float, *, sector: str | None = None) -> float:
        return calibrate_probability(score, sector=sector, buckets=self.buckets)

    def apply(self, audit: dict[str, Any]) -> dict[str, Any]:
        return apply_probability_calibration(audit, calibrator=self)


def apply_probability_calibration(
    audit: dict[str, Any],
    *,
    calibrator: ProbabilityCalibrator | None = None,
) -> dict[str, Any]:
    cal = calibrator or ProbabilityCalibrator()
    score = _sf(audit.get("next_week_score", audit.get("effective_intent_score")))
    sector = audit.get("sector") or audit.get("sector_key")
    prob = cal.predict(score, sector=str(sector) if sector else None)
    mode = _calibration_mode()
    raw_conf = _sf(audit.get("signal_confidence"))

    out: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "input_score": None if math.isnan(score) else round(score, 2),
        "predicted_probability": None if math.isnan(prob) else prob,
        "predicted_success_probability": None if math.isnan(prob) else prob,
        "signal_probability": None if math.isnan(prob) else prob,
        "raw_confidence": None if math.isnan(raw_conf) else raw_conf,
    }
    audit["probability_calibration"] = out
    audit["predicted_probability"] = prob if not math.isnan(prob) else None
    audit["predicted_success_probability"] = audit["predicted_probability"]
    audit["signal_probability"] = audit["predicted_probability"]

    if mode == "enforce" and not math.isnan(prob):
        audit["signal_confidence"] = round(prob, 3)

    return out


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    if not predictions or len(predictions) != len(outcomes):
        return float("nan")
    total = 0.0
    n = 0
    for p, y in zip(predictions, outcomes):
        if math.isnan(p):
            continue
        total += (p - float(y)) ** 2
        n += 1
    return total / n if n else float("nan")
