"""Shared legacy / shadow / enforce rollout helpers for Titan v2 review flags."""

from __future__ import annotations

import os

ROLLOUT_MODES = ("off", "shadow", "enforce")


def env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def rollout_mode(
    enable_env: str,
    mode_env: str,
    *,
    default_mode: str = "shadow",
) -> str:
    if not env_truthy(enable_env, default=False):
        return "off"
    raw = (os.environ.get(mode_env, "") or "").strip().lower()
    if raw in ("shadow", "enforce"):
        return raw
    return default_mode if default_mode in ("shadow", "enforce") else "shadow"
