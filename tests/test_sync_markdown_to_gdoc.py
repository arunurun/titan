"""Tests for markdown-to-Google-Doc sync helper script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_sync_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "sync_markdown_to_gdoc.py"
    spec = importlib.util.spec_from_file_location("sync_markdown_to_gdoc", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load sync_markdown_to_gdoc module spec.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_markdown_to_text_keeps_structure():
    mod = _load_sync_module()
    markdown = (
        "# Title\n\n"
        "- item one\n"
        "- item two\n\n"
        "See [Titan](https://example.com).\n\n"
        "```python\nprint('x')\n```\n"
    )
    text = mod.markdown_to_text(markdown)
    assert "Title" in text
    assert "• item one" in text
    assert "Titan (https://example.com)" in text
    assert "print('x')" in text
    assert "#" not in text


def test_doc_id_resolution_prefers_arg(monkeypatch):
    mod = _load_sync_module()
    monkeypatch.setenv("GOOGLE_DOC_ID", "env-doc")
    args = mod.parse_args(["--doc-id", "arg-doc", "--dry-run"])
    assert mod.resolve_doc_id(args) == "arg-doc"


def test_missing_doc_id_raises(monkeypatch):
    mod = _load_sync_module()
    monkeypatch.delenv("GOOGLE_DOC_ID", raising=False)
    args = mod.parse_args([])
    with pytest.raises(RuntimeError, match="Missing Google Doc ID"):
        mod.resolve_doc_id(args)
