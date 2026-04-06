"""Gemini narrative generation with TITAN V12.0 protocol (google-genai SDK)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types

try:
    from google.genai.errors import APIError as GenaiAPIError
except ImportError:
    GenaiAPIError = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

from compliance import compliance_scan
from config_loader import parse_gemini_api_keys_from_env
from json_util import sanitize_for_json

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


def _resolve_gemini_keys(
    api_keys: Sequence[str] | None,
    api_key: str | None,
) -> list[str]:
    if api_keys is not None and len(api_keys) > 0:
        return [str(k).strip() for k in api_keys if str(k).strip()]
    if api_key and str(api_key).strip():
        return [api_key.strip()]
    return list(parse_gemini_api_keys_from_env())


def _is_retryable_gemini_error(err_s: str) -> bool:
    """Quota, rate limits, and transient server/load errors (retry with backoff or next key)."""
    lower = err_s.lower()
    if "429" in err_s or "RESOURCE_EXHAUSTED" in err_s or "quota" in lower:
        return True
    # 503 UNAVAILABLE / model overload — common on flash-lite free tier peaks
    if "503" in err_s or "UNAVAILABLE" in err_s or "high demand" in lower:
        return True
    if "502" in err_s or "504" in err_s or "timeout" in lower:
        return True
    return False


_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retryable_gemini_exception(exc: BaseException) -> bool:
    """
    Detect retryable errors even when message text is odd or nested (SDK / tenacity).
    google.genai.errors.APIError uses .code (HTTP status), not .status_code.
    """
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        if GenaiAPIError is not None and isinstance(cur, GenaiAPIError):
            code = getattr(cur, "code", None)
            if isinstance(code, int) and code in _RETRYABLE_HTTP_CODES:
                return True
        code = getattr(cur, "status_code", None)
        if isinstance(code, int) and code in _RETRYABLE_HTTP_CODES:
            return True
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None

    for e in chain:
        if _is_retryable_gemini_error(str(e)):
            return True
    return False


def _generate(keys: list[str], model: str, user_text: str) -> str:
    """
    Try keys in order. On quota (429) or transient API errors (503 overload, etc.),
    switch to the next key when available; otherwise backoff and retry.
    """
    if not keys:
        raise ValueError("No Gemini API keys configured")
    last_err: Exception | None = None
    for ki, api_key in enumerate(keys):
        client = _make_client(api_key)
        for attempt in range(6):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=user_text,
                    config=_GEN_CONFIG,
                )
                return (resp.text or "").strip()
            except Exception as e:
                last_err = e
                if not _is_retryable_gemini_exception(e):
                    raise
                err_s = str(e)
                if ki + 1 < len(keys):
                    logger.info(
                        "Gemini transient/quota error on key %s/%s; trying next API key",
                        ki + 1,
                        len(keys),
                    )
                    break
                if attempt >= 5:
                    raise
                delay = 15.0 * (attempt + 1)
                m = re.search(r"retry in ([0-9.]+)s", err_s, re.I)
                if m:
                    delay = max(delay, float(m.group(1)) + 2.0)
                time.sleep(min(delay, 120.0))
    assert last_err is not None
    raise last_err


def generate_sector_digest_narrative(
    constituents: list[dict[str, Any]],
    *,
    sector_id: str,
    model_name: str | None = None,
    api_key: str | None = None,
    api_keys: Sequence[str] | None = None,
) -> str:
    """
    Single Gemini call for a whole sector (free-tier friendly: 1 request vs 1 per symbol).
    Pass compact per-stock metric dicts (e.g. output of equity audits without narrative).
    """
    compact = [
        {
            "symbol": a.get("symbol"),
            "exchange": a.get("exchange"),
            "z_score": a.get("z_score"),
            "absorption_ratio": a.get("absorption_ratio"),
            "intent_score": a.get("intent_score"),
            "rows": a.get("rows"),
        }
        for a in constituents
    ]
    audit_data: dict[str, Any] = {
        "sector_mode": True,
        "sector_digest": True,
        "sector": sector_id,
        "constituent_count": len(compact),
        "constituents": compact,
    }
    return generate_titan_narrative(
        audit_data,
        model_name=model_name,
        api_key=api_key,
        api_keys=api_keys,
    )


def generate_titan_narrative(
    audit_data: dict[str, Any],
    *,
    model_name: str | None = None,
    api_key: str | None = None,
    api_keys: Sequence[str] | None = None,
) -> str:
    """
    Produce a forensic narrative from audit_data using Gemini.
    Runs a deterministic policy check; raises if violations remain after one retry.
    Model: pass model_name, or set GEMINI_MODEL (default gemini-2.5-flash-lite).
    Pass api_keys (or set GEMINI_API_KEY / GEMINI_API_KEYS / GEMINI_API_KEY_2 in env) to rotate on 429.
    """
    resolved_model = (model_name or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
    keys = _resolve_gemini_keys(api_keys, api_key)
    prompt = (
        "Audit payload (JSON):\n"
        + json.dumps(sanitize_for_json(audit_data), default=str, indent=2)
        + "\n\nRespond with the post body only."
    )

    def _gen(user_text: str) -> str:
        try:
            return _generate(keys, resolved_model, user_text)
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
