"""Shared factor result contract for independent engines and titan_fusion."""

from __future__ import annotations

from typing import Any, TypedDict


class FactorResult(TypedDict):
    score: float | None  # 0-100
    confidence: float  # 0.0-1.0
    reasons: list[str]
    metadata: dict[str, Any]
    available: bool
