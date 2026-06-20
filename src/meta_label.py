"""Phase 3: LightGBM meta-label BUY veto filter.

Secondary classifier on top of signal_v2 labels. Only vetoes ``buy`` → ``accumulate``;
never blocks ``accumulate`` or ``hold``.

Features: ``risk_net``, market ``regime``, sector rank, CMF, VPR, intent, next_week.

Optional dependency: ``lightgbm`` (``pip install lightgbm``). When LightGBM is missing
or no labeled cohort has been trained, a deterministic rule-based fallback is used.
"""

from __future__ import annotations

import math
from typing import Any

FEATURE_KEYS: tuple[str, ...] = (
    "risk_net",
    "regime",
    "sector_rank",
    "cmf_20",
    "volume_participation_ratio",
    "effective_intent_score",
    "next_week_score",
)

_REGIME_ORDER: dict[str, int] = {
    "STRONG_BULL": 4,
    "BULL": 3,
    "NEUTRAL": 2,
    "DEFENSIVE": 1,
    "BEAR": 0,
}

_MIN_LABELED_COHORT = 30
_DEFAULT_LGBM_THRESHOLD = 0.45

_lgbm_available: bool | None = None
_lgbm_import_error: str | None = None


def lightgbm_available() -> bool:
    """True when the optional ``lightgbm`` package is importable."""
    global _lgbm_available, _lgbm_import_error
    if _lgbm_available is None:
        try:
            import lightgbm  # noqa: F401

            _lgbm_available = True
            _lgbm_import_error = None
        except ImportError as exc:
            _lgbm_available = False
            _lgbm_import_error = str(exc)
    return _lgbm_available


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _regime_label(audit: dict[str, Any]) -> str:
    payload = audit.get("market_regime")
    if isinstance(payload, dict):
        raw = payload.get("regime")
        if raw:
            return str(raw).strip().upper()
    raw = audit.get("market_regime")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    return "NEUTRAL"


def _sector_rank(audit: dict[str, Any]) -> float:
    for key in (
        "sector_relative_rank_score",
        "rank_score",
        "sector_pctile_next_week_score",
        "sector_pctile_effective_intent",
    ):
        v = _sf(audit.get(key))
        if not math.isnan(v):
            return v
    return float("nan")


def _vpr(audit: dict[str, Any]) -> float:
    for key in (
        "volume_participation_ratio",
        "volume_participation_for_scoring",
        "absorption_ratio",
        "absorption_for_scoring",
    ):
        v = _sf(audit.get(key))
        if not math.isnan(v):
            return v
    return float("nan")


def extract_features(audit: dict[str, Any], *, risk_net: float) -> dict[str, Any]:
    """Build the meta-label feature vector from audit + resolved risk_net."""
    intent = _sf(audit.get("effective_intent_score", audit.get("intent_score")))
    next_week = _sf(audit.get("next_week_score"))
    return {
        "risk_net": _sf(risk_net),
        "regime": _regime_label(audit),
        "sector_rank": _sector_rank(audit),
        "cmf_20": _sf(audit.get("cmf_20")),
        "volume_participation_ratio": _vpr(audit),
        "effective_intent_score": intent,
        "next_week_score": next_week,
    }


def _feature_vector(features: dict[str, Any]) -> list[float]:
    regime = str(features.get("regime") or "NEUTRAL").strip().upper()
    return [
        _sf(features.get("risk_net")),
        float(_REGIME_ORDER.get(regime, 2)),
        _sf(features.get("sector_rank")),
        _sf(features.get("cmf_20")),
        _sf(features.get("volume_participation_ratio")),
        _sf(features.get("effective_intent_score")),
        _sf(features.get("next_week_score")),
    ]


def rule_based_veto(features: dict[str, Any]) -> tuple[bool, list[str]]:
    """Deterministic fallback when LightGBM is unavailable or untrained."""
    reasons: list[str] = []
    weak = 0

    cmf = _sf(features.get("cmf_20"))
    vpr = _sf(features.get("volume_participation_ratio"))
    sector_rank = _sf(features.get("sector_rank"))
    intent = _sf(features.get("effective_intent_score"))
    next_week = _sf(features.get("next_week_score"))
    risk_net = _sf(features.get("risk_net"))
    regime = str(features.get("regime") or "NEUTRAL").strip().upper()

    if not math.isnan(cmf) and cmf < 0.0:
        weak += 1
        reasons.append("cmf_negative")
    if not math.isnan(vpr) and vpr < 0.95:
        weak += 1
        reasons.append("vpr_weak")
    if not math.isnan(sector_rank) and sector_rank < 40.0:
        weak += 1
        reasons.append("sector_rank_low")
    if not math.isnan(next_week) and next_week < 68.0:
        weak += 1
        reasons.append("next_week_borderline")
    if not math.isnan(intent) and intent < 62.0:
        weak += 1
        reasons.append("intent_borderline")
    if regime in ("DEFENSIVE", "BEAR"):
        weak += 1
        reasons.append(f"regime_{regime.lower()}")
    if not math.isnan(risk_net) and risk_net >= 2.5:
        weak += 1
        reasons.append("risk_net_elevated")

    if (
        not math.isnan(cmf)
        and cmf >= 0.08
        and not math.isnan(vpr)
        and vpr >= 1.1
        and not math.isnan(sector_rank)
        and sector_rank >= 55.0
        and not math.isnan(next_week)
        and next_week >= 72.0
    ):
        return False, ["strong_conviction_pass"]

    veto = weak >= 2
    if veto and not reasons:
        reasons.append("composite_weak")
    return veto, reasons


class MetaLabelModel:
    """LightGBM binary classifier stub with rule-based fallback."""

    def __init__(self, *, threshold: float = _DEFAULT_LGBM_THRESHOLD) -> None:
        self.threshold = threshold
        self._model: Any = None
        self._fitted = False
        self._train_rows = 0

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(self, rows: list[dict[str, Any]], outcomes: list[int]) -> bool:
        """Train on labeled cohort; returns True when LightGBM model is active."""
        if len(rows) != len(outcomes) or len(rows) < _MIN_LABELED_COHORT:
            self._fitted = False
            self._model = None
            self._train_rows = len(rows)
            return False
        if not lightgbm_available():
            self._fitted = False
            self._model = None
            self._train_rows = len(rows)
            return False

        import lightgbm as lgb
        import numpy as np

        x = np.array([_feature_vector(r) for r in rows], dtype=float)
        y = np.array(outcomes, dtype=int)
        if len(set(int(v) for v in y)) < 2:
            self._fitted = False
            self._model = None
            self._train_rows = len(rows)
            return False

        self._model = lgb.LGBMClassifier(
            n_estimators=64,
            max_depth=4,
            learning_rate=0.08,
            min_child_samples=max(5, len(rows) // 20),
            random_state=42,
            verbose=-1,
        )
        self._model.fit(x, y)
        self._fitted = True
        self._train_rows = len(rows)
        return True

    def predict_pass_probability(self, features: dict[str, Any]) -> float | None:
        if not self._fitted or self._model is None:
            return None
        import numpy as np

        x = np.array([_feature_vector(features)], dtype=float)
        proba = self._model.predict_proba(x)[0]
        return float(proba[1]) if len(proba) > 1 else float(proba[0])

    def should_veto(self, features: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        prob = self.predict_pass_probability(features)
        if prob is not None:
            veto = prob < self.threshold
            detail = {
                "method": "lightgbm",
                "pass_probability": round(prob, 4),
                "threshold": self.threshold,
                "trained_rows": self._train_rows,
            }
            reason = "lgbm_low_confidence" if veto else "lgbm_pass"
            return veto, reason, detail

        veto, reasons = rule_based_veto(features)
        detail = {
            "method": "rule_fallback",
            "weak_signals": reasons,
            "lightgbm_available": lightgbm_available(),
            "trained_rows": self._train_rows,
        }
        reason = reasons[0] if veto and reasons else "rule_pass"
        return veto, reason, detail


_default_model: MetaLabelModel | None = None


def get_meta_label_model() -> MetaLabelModel:
    global _default_model
    if _default_model is None:
        _default_model = MetaLabelModel()
    return _default_model


def apply_meta_label_veto(
    label: str,
    audit: dict[str, Any],
    *,
    risk_net: float,
    model: MetaLabelModel | None = None,
) -> str:
    """Veto ``buy`` only; downgrade to ``accumulate`` when meta-label rejects."""
    final = str(label or "").strip().lower()
    features = extract_features(audit, risk_net=risk_net)
    ml = model or get_meta_label_model()

    if final != "buy":
        audit["meta_label"] = {
            "applied": False,
            "label_in": final,
            "label_out": final,
            "reason": "not_buy",
            "features": features,
            "feature_keys": list(FEATURE_KEYS),
        }
        return final

    veto, reason, detail = ml.should_veto(features)
    out = "accumulate" if veto else "buy"
    audit["meta_label"] = {
        "applied": veto,
        "label_in": "buy",
        "label_out": out,
        "reason": reason,
        "features": features,
        "feature_keys": list(FEATURE_KEYS),
        **detail,
    }
    return out
