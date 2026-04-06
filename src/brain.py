"""Gemini narrative generation with TITAN V12.0 protocol (google-genai SDK)."""

from __future__ import annotations

import json
import os
from typing import Any

from google import genai
from google.genai import types

from compliance import compliance_scan

TITAN_V12_SYSTEM_INSTRUCTION = """You are Titan V12.0 Forensic Analyst.
Protocol:
- Describe market structure, positioning, and risk context only.
- Never give investment advice, price targets, entries, or exits.
- Never use the words Buy, Sell, Target, SL, or Stop Loss.
- Output a single concise post suitable for X/LinkedIn (plain text).
- After drafting, mentally verify policy compliance before answering."""

_GEN_CONFIG = types.GenerateContentConfig(
    system_instruction=TITAN_V12_SYSTEM_INSTRUCTION,
)


def _policy_check_passes(text: str) -> bool:
    ok, _ = compliance_scan(text)
    return ok


def _make_client(api_key: str | None) -> genai.Client:
    if api_key:
        return genai.Client(api_key=api_key)
    return genai.Client()


def _generate(client: genai.Client, model: str, user_text: str) -> str:
    resp = client.models.generate_content(
        model=model,
        contents=user_text,
        config=_GEN_CONFIG,
    )
    return (resp.text or "").strip()


def generate_titan_narrative(
    audit_data: dict[str, Any],
    *,
    model_name: str | None = None,
    api_key: str | None = None,
) -> str:
    """
    Produce a forensic narrative from audit_data using Gemini.
    Runs a deterministic policy check; raises if violations remain after one retry.
    Model: pass model_name, or set GEMINI_MODEL (default gemini-2.5-flash-lite).
    """
    resolved_model = (model_name or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
    client = _make_client(api_key)
    prompt = (
        "Audit payload (JSON):\n"
        + json.dumps(audit_data, default=str, indent=2)
        + "\n\nRespond with the post body only."
    )

    def _gen(user_text: str) -> str:
        try:
            return _generate(client, resolved_model, user_text)
        except Exception as e:
            raise RuntimeError(f"[Gemini] {e}") from e

    text = _gen(prompt)
    if _policy_check_passes(text):
        return text
    repair = (
        "Your previous draft failed compliance (forbidden wording). "
        "Rewrite the same insights using neutral forensic language only. "
        "Return the post body only.\n\nPrevious draft:\n"
        + text
    )
    text2 = _gen(repair)
    if _policy_check_passes(text2):
        return text2
    raise ValueError("[Gemini] Policy check failed after retry; narrative blocked.")
