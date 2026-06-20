"""Historical score → P(up 5d) calibration layer (always-on production logic).

Walk-forward isotonic regression on labeled cohort features maps audit inputs to
``predicted_probability``. When labeled data is insufficient, bucket interpolation
on ``next_week_score`` / intent is used as fallback.

``technical_confidence`` from the signal engine is preserved separately on
``signal_confidence``; it is not overwritten by calibration output.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

_DEFAULT_BUCKETS: tuple[tuple[float, float, float], ...] = (
    (0.0, 45.0, 0.28),
    (45.0, 49.0, 0.30),
    (49.0, 52.0, 0.32),
    (52.0, 55.0, 0.34),
    (55.0, 58.0, 0.36),
    (58.0, 62.0, 0.39),
    (62.0, 65.0, 0.42),
    (65.0, 68.0, 0.45),
    (68.0, 72.0, 0.48),
    (72.0, 76.0, 0.52),
    (76.0, 80.0, 0.55),
    (80.0, 100.0, 0.62),
)

_ISOTONIC_FEATURE_KEYS: tuple[str, ...] = (
    "next_week_score",
    "effective_intent_score",
    "risk_net",
    "sector",
    "market_regime",
    "volume_participation_ratio",
    "cmf_20",
)

MIN_ISOTONIC_SAMPLES: int = 30

_REGIME_SCORE_ADJ: dict[str, float] = {
    "STRONG_BULL": 3.0,
    "BULL": 2.0,
    "NEUTRAL": 0.0,
    "DEFENSIVE": -2.0,
    "BEAR": -4.0,
}


def calibration_mode() -> str:
    """Production path always applies calibration (isotonic or bucket fallback)."""
    return "enforce"


def calibration_enabled() -> bool:
    return True


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sector_score_adj(sector: str | None) -> float:
    if not sector:
        return 0.0
    key = str(sector).strip().lower()
    if key in ("telecom", "defence"):
        return 3.0
    if key in ("pharma_healthcare",):
        return -2.0
    return 0.0


def _regime_score_adj(regime: str | None) -> float:
    if not regime:
        return 0.0
    return _REGIME_SCORE_ADJ.get(str(regime).strip().upper(), 0.0)


def extract_isotonic_features(audit: dict[str, Any]) -> dict[str, Any]:
    """Extract Phase 2 isotonic feature vector from an audit dict."""
    regime_raw = audit.get("market_regime")
    if isinstance(regime_raw, dict):
        regime_val = regime_raw.get("regime") or regime_raw.get("label")
    else:
        regime_val = regime_raw

    risk = audit.get("risk_net")
    if risk is None:
        risk = audit.get("sell_signal_risk_score")

    sector = audit.get("sector") or audit.get("sector_key")

    return {
        "next_week_score": _sf(audit.get("next_week_score")),
        "effective_intent_score": _sf(audit.get("effective_intent_score")),
        "risk_net": _sf(risk),
        "sector": str(sector).strip().lower() if sector else "",
        "market_regime": str(regime_val or "").strip().upper(),
        "volume_participation_ratio": _sf(audit.get("volume_participation_ratio")),
        "cmf_20": _sf(audit.get("cmf_20")),
    }


def composite_calibration_input(audit: dict[str, Any]) -> float:
    """Scalar score fed to isotonic regression (multi-feature composite)."""
    feats = extract_isotonic_features(audit)
    base = feats["next_week_score"]
    if math.isnan(base):
        base = feats["effective_intent_score"]
    if math.isnan(base):
        return float("nan")

    adj = 0.0
    intent = feats["effective_intent_score"]
    if not math.isnan(intent):
        adj += (intent - 50.0) * 0.15

    risk = feats["risk_net"]
    if not math.isnan(risk):
        adj -= risk * 2.5

    adj += _sector_score_adj(feats["sector"] or None)
    adj += _regime_score_adj(feats["market_regime"] or None)

    vpr = feats["volume_participation_ratio"]
    if not math.isnan(vpr):
        adj += _clamp((vpr - 1.0) * 3.0, -2.0, 4.0)

    cmf = feats["cmf_20"]
    if not math.isnan(cmf):
        adj += cmf * 8.0

    return round(_clamp(base + adj, 0.0, 100.0), 4)


class _IsoBlock:
    __slots__ = ("x_min", "x_max", "sum_y", "w")

    def __init__(self, x: float, y: float, w: float = 1.0) -> None:
        self.x_min = x
        self.x_max = x
        self.sum_y = y * w
        self.w = w

    @property
    def avg(self) -> float:
        return self.sum_y / self.w if self.w else 0.0

    def merge(self, other: _IsoBlock) -> None:
        self.x_max = other.x_max
        self.sum_y += other.sum_y
        self.w += other.w


def _fit_pav(x: list[float], y: list[float]) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators isotonic regression; returns (x_breaks, y_hat)."""
    if not x:
        return [], []
    order = np.argsort(np.asarray(x, dtype=float), kind="mergesort")
    xs = [float(x[i]) for i in order]
    ys = [float(y[i]) for i in order]

    blocks: list[_IsoBlock] = [_IsoBlock(xi, yi) for xi, yi in zip(xs, ys)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i].avg <= blocks[i + 1].avg + 1e-12:
            i += 1
            continue
        blocks[i].merge(blocks[i + 1])
        del blocks[i + 1]
        if i:
            i -= 1

    xp: list[float] = []
    yp: list[float] = []
    for block in blocks:
        xp.append(block.x_min)
        yp.append(_clamp(block.avg, 0.0, 1.0))
        if block.x_max > block.x_min:
            xp.append(block.x_max)
            yp.append(_clamp(block.avg, 0.0, 1.0))
    return xp, yp


def _predict_pav(x_query: float, xp: list[float], yp: list[float]) -> float:
    if not xp or math.isnan(x_query):
        return float("nan")
    val = float(np.interp(float(x_query), np.asarray(xp, dtype=float), np.asarray(yp, dtype=float)))
    return round(_clamp(val, 0.0, 1.0), 4)


def compute_position_score(
    predicted_probability: float | None,
    technical_confidence: float | None,
    *,
    prob_weight: float = 0.6,
    conf_weight: float = 0.4,
) -> float | None:
    """Blend calibrated probability with engine technical confidence for sizing."""
    p = _sf(predicted_probability) if predicted_probability is not None else float("nan")
    c = _sf(technical_confidence) if technical_confidence is not None else float("nan")
    if math.isnan(p) and math.isnan(c):
        return None
    if math.isnan(p):
        return round(_clamp(c, 0.0, 1.0), 4)
    if math.isnan(c):
        return round(_clamp(p, 0.0, 1.0), 4)
    return round(_clamp(prob_weight * p + conf_weight * c, 0.0, 1.0), 4)


def _sector_prob_adj(sector: str | None) -> float:
    if not sector:
        return 0.0
    key = str(sector).strip().lower()
    if key in ("telecom", "defence"):
        return 0.03
    if key in ("pharma_healthcare",):
        return -0.02
    return 0.0


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
    sector_adj = _sector_prob_adj(sector)
    for lo, hi, prob in table:
        if lo <= s < hi or (hi == table[-1][1] and s >= lo):
            return round(_clamp(prob + sector_adj, 0.0, 1.0), 4)
    return round(_clamp(table[-1][2] + sector_adj, 0.0, 1.0), 4)


class IsotonicCalibrator:
    """Walk-forward isotonic regression on labeled audit cohort."""

    feature_keys: tuple[str, ...] = _ISOTONIC_FEATURE_KEYS

    def __init__(self, *, min_train: int = MIN_ISOTONIC_SAMPLES) -> None:
        self.min_train = min_train
        self._fitted = False
        self._xp: list[float] = []
        self._yp: list[float] = []
        self._n_train = 0

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def n_train(self) -> int:
        return self._n_train

    def _input_score(self, audit: dict[str, Any]) -> float:
        return composite_calibration_input(audit)

    @staticmethod
    def _order_walk_forward(
        rows: list[dict[str, Any]],
        outcomes: list[int],
    ) -> tuple[list[dict[str, Any]], list[int]]:
        indexed = list(enumerate(zip(rows, outcomes)))
        indexed.sort(key=lambda item: str(item[1][0].get("trade_date") or item[0]))
        rows_o = [pair[0] for _, pair in indexed]
        out_o = [pair[1] for _, pair in indexed]
        return rows_o, out_o

    def fit(
        self,
        rows: list[dict[str, Any]],
        outcomes: list[int],
        *,
        walk_forward: bool = False,
    ) -> bool:
        if walk_forward:
            return self.fit_walk_forward(rows, outcomes)
        if len(rows) != len(outcomes):
            self._fitted = False
            return False

        xs: list[float] = []
        ys: list[float] = []
        for row, outcome in zip(rows, outcomes):
            score = self._input_score(row)
            if math.isnan(score):
                continue
            xs.append(score)
            ys.append(float(int(outcome)))

        if len(xs) < self.min_train:
            self._fitted = False
            self._xp, self._yp = [], []
            self._n_train = 0
            return False

        self._xp, self._yp = _fit_pav(xs, ys)
        self._fitted = bool(self._xp)
        self._n_train = len(xs)
        return self._fitted

    def fit_walk_forward(
        self,
        rows: list[dict[str, Any]],
        outcomes: list[int],
        *,
        min_folds: int = 2,
    ) -> bool:
        """Expanding-window walk-forward; final fit uses all labeled rows when valid."""
        if len(rows) != len(outcomes) or len(rows) < self.min_train:
            self._fitted = False
            return False

        rows_o, outcomes_o = self._order_walk_forward(rows, outcomes)
        n = len(rows_o)
        step = max(1, (n - self.min_train) // max(1, min_folds))
        ok_folds = 0
        for end in range(self.min_train, n + 1, step):
            probe = IsotonicCalibrator(min_train=self.min_train)
            if probe.fit(rows_o[:end], outcomes_o[:end]):
                ok_folds += 1

        if ok_folds < 1:
            self._fitted = False
            return False

        return self.fit(rows_o, outcomes_o, walk_forward=False)

    def predict(self, audit: dict[str, Any]) -> float:
        if not self._fitted:
            return float("nan")
        score = self._input_score(audit)
        return _predict_pav(score, self._xp, self._yp)


class ProbabilityCalibrator:
    def __init__(
        self,
        *,
        buckets: tuple[tuple[float, float, float], ...] | None = None,
        isotonic: IsotonicCalibrator | None = None,
    ) -> None:
        self.buckets = buckets or _DEFAULT_BUCKETS
        self.isotonic = isotonic if isotonic is not None else IsotonicCalibrator()

    def calibration_method(self) -> str:
        return "isotonic" if self.isotonic.is_fitted else "bucket"

    def predict(
        self,
        score: float,
        *,
        sector: str | None = None,
        audit: dict[str, Any] | None = None,
    ) -> float:
        if self.isotonic.is_fitted and audit is not None:
            prob = self.isotonic.predict(audit)
            if not math.isnan(prob):
                return prob
        return calibrate_probability(score, sector=sector, buckets=self.buckets)

    def apply(self, audit: dict[str, Any]) -> dict[str, Any]:
        return apply_probability_calibration(audit, calibrator=self)


def apply_calibration(
    label: str,
    raw_score: float,
    audit: dict[str, Any],
    *,
    calibrator: ProbabilityCalibrator | None = None,
) -> float:
    """Calibrate raw score to P(up 5d); short-circuit for structural exit-risk bypass."""
    trace = audit.get("signal_reason_trace") or {}
    forced = audit.get("forced_label") or trace.get("forced_label")
    bypass = bool(audit.get("bypass_hysteresis") or trace.get("bypass_hysteresis"))
    if forced == "exit-risk" and bypass:
        return 1.0
    cal = calibrator or ProbabilityCalibrator()
    sector = audit.get("sector") or audit.get("sector_key")
    return cal.predict(
        raw_score,
        sector=str(sector) if sector else None,
        audit=audit,
    )


def apply_probability_calibration(
    audit: dict[str, Any],
    *,
    calibrator: ProbabilityCalibrator | None = None,
) -> dict[str, Any]:
    cal = calibrator or ProbabilityCalibrator()
    score = _sf(audit.get("next_week_score", audit.get("effective_intent_score")))
    sector = audit.get("sector") or audit.get("sector_key")
    label = str(audit.get("action_signal") or audit.get("sell_signal") or "")
    tech_conf = _sf(audit.get("signal_confidence"))

    if not math.isnan(tech_conf):
        audit["technical_confidence"] = round(tech_conf, 3)

    prob = apply_calibration(label, score, audit, calibrator=cal)
    method = cal.calibration_method()

    out = {
        "enabled": True,
        "mode": "enforce",
        "method": method,
        "input_score": None if math.isnan(score) else round(score, 2),
        "predicted_probability": None if math.isnan(prob) else prob,
        "predicted_success_probability": None if math.isnan(prob) else prob,
        "signal_probability": None if math.isnan(prob) else prob,
        "technical_confidence": audit.get("technical_confidence"),
        "isotonic_phase2_features": list(_ISOTONIC_FEATURE_KEYS),
        "isotonic_trained": cal.isotonic.is_fitted,
        "isotonic_n_train": cal.isotonic.n_train if cal.isotonic.is_fitted else 0,
        "exit_risk_bypass": prob == 1.0 and (
            (audit.get("forced_label") or (audit.get("signal_reason_trace") or {}).get("forced_label"))
            == "exit-risk"
        ),
    }
    audit["probability_calibration"] = out
    audit["predicted_probability"] = prob if not math.isnan(prob) else None
    audit["predicted_success_probability"] = audit["predicted_probability"]
    audit["signal_probability"] = audit["predicted_probability"]

    pos = compute_position_score(audit["predicted_probability"], audit.get("technical_confidence"))
    if pos is not None:
        audit["position_score"] = pos
        out["position_score"] = pos

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
