"""Shared pytest hooks and fixtures."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless TITAN_RUN_INTEGRATION=1 (see pytest.ini marker)."""
    if os.environ.get("TITAN_RUN_INTEGRATION"):
        return
    skip = pytest.mark.skip(reason="integration tests disabled (set TITAN_RUN_INTEGRATION=1)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
