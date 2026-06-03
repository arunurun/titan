"""Print legacy vs v2 label matrix for cited tickers (Task 8 deliverable).

Run: python -m pytest tests/test_ticker_matrix_report.py -s -q
Or:  python tests/test_ticker_matrix_report.py
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

# Allow `python tests/test_ticker_matrix_report.py` without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from action_signals import _derive_action_signal_legacy, derive_action_signal  # noqa: E402
from signal_v2_ticker_fixtures import TICKER_CASES  # noqa: E402


def _top_reason(audit: dict) -> str:
    trace = audit.get("signal_reason_trace") or {}
    for key in ("modifiers", "data_quality"):
        reasons = trace.get(key) or []
        if reasons:
            return str(reasons[0])
    forced = trace.get("forced_label")
    if forced:
        return f"forced_label={forced}"
    terms = trace.get("terms") or []
    if terms:
        t0 = terms[0]
        return f"{t0.get('group')} {t0.get('metric')} ({t0.get('side')})"
    return "stable"


def build_matrix_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for case in TICKER_CASES:
        audit = copy.deepcopy(case["audit"])
        legacy_label, _, _ = _derive_action_signal_legacy(copy.deepcopy(case["audit"]))
        os.environ["TITAN_SIGNAL_V2"] = "1"
        os.environ["TITAN_SIGV2_ENABLE_ACCUMULATE"] = "1"
        v2_label, _, _ = derive_action_signal(audit)
        conf = audit.get("signal_confidence", "")
        rows.append(
            {
                "ticker": case["ticker"],
                "legacy": legacy_label,
                "v2": v2_label,
                "confidence": str(conf),
                "top_reason": _top_reason(audit),
            }
        )
    return rows


def format_markdown_table(rows: list[dict[str, str]]) -> str:
    header = "| ticker | legacy | v2 | confidence | top reason |"
    sep = "|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        reason = r["top_reason"].replace("|", "/")
        lines.append(
            f"| {r['ticker']} | {r['legacy']} | {r['v2']} | {r['confidence']} | {reason} |"
        )
    return "\n".join(lines)


def test_ticker_matrix_report(capsys):
    """Emits the markdown comparison table when run with pytest -s."""
    table = format_markdown_table(build_matrix_rows())
    print("\n" + table + "\n")
    captured = capsys.readouterr()
    assert "SYRMA" in captured.out
    assert "| legacy | v2 |" in captured.out


if __name__ == "__main__":
    print(format_markdown_table(build_matrix_rows()))
