#!/usr/bin/env python3
"""CLI: calibrate titan_fusion pillar weights from historical audit rows.

Examples:
  python scripts/feature_calibration_report.py --json
  python scripts/feature_calibration_report.py --method correlation --output data/calibration/recommended_weights.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
_SCRIPTS = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if p != str(_SCRIPTS)]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feature_calibration import (  # noqa: E402
    format_calibration_report,
    suggest_titan_weights,
    write_recommended_weights,
)

DEFAULT_OUTPUT = ROOT / "data" / "recommended_weights.json"


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Titan fusion feature-weight calibration (Phase 3).")
    ap.add_argument("--input", type=Path, default=None, help="JSON array of historical audit rows")
    ap.add_argument(
        "--method",
        default="auto",
        choices=("auto", "rf", "xgb", "lgbm", "correlation"),
        help="importance method (auto tries optional ML libs then correlation)",
    )
    ap.add_argument("--label-key", default="forward_return_5d_up", help="outcome column name")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="recommended_weights.json path")
    ap.add_argument("--json", action="store_true", help="emit JSON report to stdout")
    return ap.parse_args(argv)


def _load_rows(path: Path | None) -> list[dict]:
    if path is None:
        return _synthetic_rows()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return list(data["rows"])
    if isinstance(data, list):
        return list(data)
    raise SystemExit(f"expected JSON array or {{rows: [...]}} in {path}")


def _synthetic_rows() -> list[dict]:
    """Minimal demo cohort when no --input is provided."""
    rows: list[dict] = []
    for i in range(40):
        tech = 45.0 + (i % 10) * 3.0
        rel = 40.0 + (i % 7) * 5.0
        fund = 50.0 + (i % 5)
        flow = 48.0 + (i % 6)
        regime = 72.0 if i % 2 == 0 else 58.0
        risk = 80.0 - (i % 4) * 2.0
        up = tech > 55 and rel > 50
        rows.append(
            {
                "factor_scores": {
                    "technical": {"score": tech, "available": True},
                    "relative_strength": {"score": rel, "available": True},
                    "institutional_flow": {"score": flow, "available": True},
                    "fundamentals": {"score": fund, "available": True},
                    "market_regime": {"score": regime, "available": True},
                    "sector_strength": {"score": rel, "available": True},
                    "risk": {"score": risk, "available": True},
                },
                "forward_return_5d_up": up,
            }
        )
    return rows


def main(argv=None) -> int:
    args = _parse_args(argv)
    rows = _load_rows(args.input)
    report = suggest_titan_weights(rows, method=args.method, label_key=args.label_key)
    out_path = write_recommended_weights(report, args.output)
    if args.json:
        report = {**report, "output_path": str(out_path)}
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_calibration_report(report))
        print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
