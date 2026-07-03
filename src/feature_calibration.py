"""Phase 3 feature calibration — suggest titan_fusion pillar weights from historical rows.

Uses optional sklearn / xgboost / lightgbm when installed; falls back to
numpy correlation weighting for smoke tests and minimal environments.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from titan_fusion import DEFAULT_FUSION_WEIGHTS, FUSION_PILLARS

_CALIBRATION_FEATURE_KEYS: tuple[str, ...] = FUSION_PILLARS


def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401

        return True
    except ImportError:
        return False


def xgboost_available() -> bool:
    try:
        import xgboost  # noqa: F401

        return True
    except ImportError:
        return False


def lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401

        return True
    except ImportError:
        return False


def _sf(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x if not math.isnan(x) else float("nan")


def _pillar_score_from_row(row: dict[str, Any], pillar: str) -> float:
    factor_scores = row.get("factor_scores")
    if isinstance(factor_scores, dict):
        block = factor_scores.get(pillar)
        if isinstance(block, dict):
            score = _sf(block.get("score"))
            if not math.isnan(score):
                return score

    fusion = row.get("titan_fusion")
    if isinstance(fusion, dict):
        key_map = {
            "technical": "technical_score",
            "relative_strength": "relative_strength_score",
            "institutional_flow": "flow_score",
            "fundamentals": "fundamental_score",
            "market_regime": "regime_score",
            "sector_strength": "sector_score",
            "risk": "risk_score",
        }
        score = _sf(fusion.get(key_map.get(pillar, "")))
        if not math.isnan(score):
            return score

    direct_map = {
        "technical": ("effective_intent_score", "intent_score"),
        "relative_strength": ("sector_relative_strength_pctile", "rel_return_20d_vs_nifty_pct"),
        "institutional_flow": (),
        "fundamentals": ("fundamental_score",),
        "market_regime": (),
        "sector_strength": ("sector_relative_strength_pctile",),
        "risk": (),
    }
    for key in direct_map.get(pillar, ()):
        score = _sf(row.get(key))
        if not math.isnan(score):
            return score

    if pillar == "institutional_flow":
        flow = row.get("institutional_flow")
        if isinstance(flow, dict):
            score = _sf(flow.get("score"))
            if not math.isnan(score):
                return score

    return float("nan")


def extract_calibration_features(row: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for pillar in _CALIBRATION_FEATURE_KEYS:
        score = _pillar_score_from_row(row, pillar)
        out[pillar] = None if math.isnan(score) else round(score, 4)
    return out


def _label_from_row(row: dict[str, Any], *, label_key: str = "forward_return_5d_up") -> float | None:
    if label_key in row:
        raw = row.get(label_key)
        if raw is None:
            return None
        if isinstance(raw, bool):
            return 1.0 if raw else 0.0
        val = _sf(raw)
        return None if math.isnan(val) else val

    for key in ("outcome_up_5d", "forward_up_5d", "label_up"):
        if key in row:
            raw = row.get(key)
            if isinstance(raw, bool):
                return 1.0 if raw else 0.0
            val = _sf(raw)
            if not math.isnan(val):
                return val

    ret = _sf(row.get("forward_return_5d_pct", row.get("return_5d_pct")))
    if not math.isnan(ret):
        return 1.0 if ret > 0 else 0.0
    return None


def build_calibration_matrix(
    rows: Sequence[dict[str, Any]],
    *,
    label_key: str = "forward_return_5d_up",
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Return (X, y, feature_names) dropping rows with incomplete labels."""
    xs: list[list[float]] = []
    ys: list[float] = []
    for row in rows:
        label = _label_from_row(row, label_key=label_key)
        if label is None:
            continue
        feats = extract_calibration_features(row)
        vec = [float(feats[p] if feats[p] is not None else float("nan")) for p in _CALIBRATION_FEATURE_KEYS]
        if any(math.isnan(v) for v in vec):
            continue
        xs.append(vec)
        ys.append(float(label))
    if not xs:
        return np.empty((0, len(_CALIBRATION_FEATURE_KEYS))), np.empty(0), list(_CALIBRATION_FEATURE_KEYS)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), list(_CALIBRATION_FEATURE_KEYS)


def _normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.get(p, 0.0) for p in FUSION_PILLARS)
    if total <= 0:
        return dict(DEFAULT_FUSION_WEIGHTS)
    weights = {p: round(raw.get(p, 0.0) / total, 6) for p in FUSION_PILLARS}
    drift = 1.0 - sum(weights.values())
    if abs(drift) > 1e-12:
        anchor = max(FUSION_PILLARS, key=lambda p: weights[p])
        weights[anchor] = round(weights[anchor] + drift, 6)
    return weights


def _correlation_weights(X: np.ndarray, y: np.ndarray, feature_names: list[str]) -> dict[str, float]:
    if X.shape[0] < 5:
        return dict(DEFAULT_FUSION_WEIGHTS)
    raw: dict[str, float] = {}
    for i, name in enumerate(feature_names):
        col = X[:, i]
        if np.std(col) < 1e-9 or np.std(y) < 1e-9:
            raw[name] = 0.0
            continue
        corr = float(np.corrcoef(col, y)[0, 1])
        raw[name] = abs(corr) if not math.isnan(corr) else 0.0
    if sum(raw.values()) <= 0:
        return dict(DEFAULT_FUSION_WEIGHTS)
    return _normalize_weights(raw)


def _importances_from_model(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    *,
    method: str,
) -> dict[str, float] | None:
    if method == "rf" and sklearn_available():
        from sklearn.ensemble import RandomForestClassifier

        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=4,
            random_state=42,
            n_jobs=1,
        )
        model.fit(X, y)
        imps = model.feature_importances_
    elif method == "xgb" and xgboost_available():
        import xgboost as xgb

        model = xgb.XGBClassifier(
            n_estimators=80,
            max_depth=3,
            learning_rate=0.1,
            objective="binary:logistic",
            random_state=42,
            n_jobs=1,
        )
        model.fit(X, y)
        imps = model.feature_importances_
    elif method == "lgbm" and lightgbm_available():
        import lightgbm as lgb

        model = lgb.LGBMClassifier(
            n_estimators=80,
            max_depth=3,
            learning_rate=0.1,
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
        model.fit(X, y)
        imps = model.feature_importances_
    else:
        return None

    raw = {feature_names[i]: float(imps[i]) for i in range(len(feature_names))}
    return _normalize_weights(raw)


def fit_feature_importance(
    rows: Sequence[dict[str, Any]],
    *,
    method: str = "auto",
    label_key: str = "forward_return_5d_up",
) -> dict[str, Any]:
    """Fit pillar importances (rf/xgb/lgbm/correlation) and return normalized weights."""
    return suggest_titan_weights(rows, method=method, label_key=label_key)


def suggest_titan_weights(
    rows: Sequence[dict[str, Any]],
    *,
    method: str = "auto",
    label_key: str = "forward_return_5d_up",
) -> dict[str, Any]:
    """
    Suggest normalized fusion pillar weights from labeled historical audits.

    ``method``: auto | rf | xgb | lgbm | correlation
    """
    X, y, feature_names = build_calibration_matrix(rows, label_key=label_key)
    n_rows = int(X.shape[0])
    if n_rows < 10:
        return {
            "weights": dict(DEFAULT_FUSION_WEIGHTS),
            "method": "default",
            "n_rows": n_rows,
            "warning": "insufficient labeled rows",
            "feature_names": feature_names,
        }

    chosen = method.strip().lower()
    if chosen == "auto":
        for candidate in ("rf", "xgb", "lgbm", "correlation"):
            if candidate == "correlation":
                weights = _correlation_weights(X, y, feature_names)
                return {
                    "weights": weights,
                    "method": "correlation",
                    "n_rows": n_rows,
                    "feature_names": feature_names,
                }
            weights = _importances_from_model(X, y, feature_names, method=candidate)
            if weights is not None:
                return {
                    "weights": weights,
                    "method": candidate,
                    "n_rows": n_rows,
                    "feature_names": feature_names,
                }
        weights = _correlation_weights(X, y, feature_names)
        return {
            "weights": weights,
            "method": "correlation",
            "n_rows": n_rows,
            "feature_names": feature_names,
        }

    if chosen == "correlation":
        weights = _correlation_weights(X, y, feature_names)
    else:
        weights = _importances_from_model(X, y, feature_names, method=chosen)
        if weights is None:
            weights = _correlation_weights(X, y, feature_names)
            chosen = "correlation"

    return {
        "weights": weights,
        "method": chosen,
        "n_rows": n_rows,
        "feature_names": feature_names,
    }


def write_recommended_weights(
    report: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Write ``recommended_weights.json`` artifact."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "weights": report.get("weights") or dict(DEFAULT_FUSION_WEIGHTS),
        "method": report.get("method"),
        "n_rows": report.get("n_rows"),
        "feature_names": report.get("feature_names"),
        "warning": report.get("warning"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def format_calibration_report(report: dict[str, Any]) -> str:
    weights = report.get("weights") or {}
    lines = [
        "=== Titan fusion weight calibration ===",
        f"method: {report.get('method')}",
        f"n_rows: {report.get('n_rows')}",
    ]
    if report.get("warning"):
        lines.append(f"warning: {report['warning']}")
    lines.append("")
    for pillar in FUSION_PILLARS:
        w = weights.get(pillar)
        if w is not None:
            lines.append(f"  {pillar}: {float(w):.4f}")
    return "\n".join(lines)
