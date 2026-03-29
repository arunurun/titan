"""SEBI-style wording scan for outbound content."""

from __future__ import annotations

import re

_DEFAULT_FORBIDDEN = (
    r"\bbuy\b",
    r"\bsell\b",
    r"\btarget\b",
    r"\bSL\b",
    r"\bstop\s*loss\b",
)


def compliance_scan(text: str, extra_patterns: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    """
    Return (ok, matched_snippets). If ok is False, at least one forbidden pattern matched.
    """
    if not text or not str(text).strip():
        return False, ["empty"]
    flags = re.IGNORECASE
    hits: list[str] = []
    for pat in (*_DEFAULT_FORBIDDEN, *extra_patterns):
        if re.search(pat, text, flags):
            hits.append(pat)
    return (len(hits) == 0), hits
