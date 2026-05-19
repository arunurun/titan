"""Gemini narrative generation with TITAN V12.0 protocol (google-genai SDK)."""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import threading
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


def _env_truthy(name: str, *, default: bool = True) -> bool:
    """Parse GEMINI_* style flags: empty -> default; 0/false/off -> False."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


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

# Disable AFC (automatic function calling): SDK default allows up to 10 extra round-trips per
# generate_content, each billed as API traffic — bad for free-tier quotas. Titan prompts are
# tool-free plain generation only.
_GEN_CONFIG = types.GenerateContentConfig(
    system_instruction=TITAN_V12_SYSTEM_INSTRUCTION,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(
        disable=True,
        maximum_remote_calls=0,
    ),
)

_GEMINI_CALL_LOCK = threading.Lock()
_LAST_GEMINI_CALL_AT = 0.0


def _min_gemini_call_interval_seconds() -> float:
    """
    Minimum wall time between generate_content calls (process-wide).
    Default 0 (no throttle). For --all-sectors on free tier, set GEMINI_MIN_CALL_INTERVAL_SECONDS=45 in CI or .env.
    """
    raw = os.environ.get("GEMINI_MIN_CALL_INTERVAL_SECONDS", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 45.0


def _gemini_generate_content(client: genai.Client, *, model: str, user_text: str) -> Any:
    """Rate-limited wrapper around models.generate_content."""
    global _LAST_GEMINI_CALL_AT
    interval = _min_gemini_call_interval_seconds()
    with _GEMINI_CALL_LOCK:
        if interval > 0:
            now = time.monotonic()
            wait = interval - (now - _LAST_GEMINI_CALL_AT)
            if wait > 0:
                logger.info("Gemini throttle: waiting %.1fs before API call", wait)
                time.sleep(wait)
        try:
            return client.models.generate_content(
                model=model,
                contents=user_text,
                config=_GEN_CONFIG,
            )
        finally:
            _LAST_GEMINI_CALL_AT = time.monotonic()


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


def _gemini_error_search_text(exc: BaseException) -> str:
    """Collect message/details/response from SDK errors for quota parsing and logging."""
    chunks: list[str] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chunks.append(str(cur))
        if GenaiAPIError is not None and isinstance(cur, GenaiAPIError):
            for attr in ("message", "details"):
                v = getattr(cur, attr, None)
                if v is not None:
                    chunks.append(str(v))
            resp = getattr(cur, "response", None)
            if resp is not None:
                chunks.append(str(resp))
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None
    return "\n".join(chunks)


def _gemini_http_status(exc: BaseException) -> int | None:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if GenaiAPIError is not None and isinstance(cur, GenaiAPIError):
            code = getattr(cur, "code", None)
            if isinstance(code, int):
                return code
        code = getattr(cur, "status_code", None)
        if isinstance(code, int):
            return code
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None
    return None


def _is_per_day_quota_exhausted(err: BaseException) -> bool:
    """
    True when the API reports daily free-tier (or per-day) generation caps.
    Retrying the same day / same project does not help; avoid long backoff loops.
    """
    err_s = _gemini_error_search_text(err)
    if "GenerateRequestsPerDayPerProjectPerModel" in err_s:
        return True
    if "GenerateRequestsPerDay" in err_s and "FreeTier" in err_s:
        return True
    low = err_s.lower()
    if "per day" in low and ("quota" in low or "exceeded" in low):
        return True
    return False


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
    seen_429_on_prior_key = False
    for ki, api_key in enumerate(keys):
        client = _make_client(api_key)
        for attempt in range(6):
            try:
                resp = _gemini_generate_content(client, model=model, user_text=user_text)
                return (resp.text or "").strip()
            except Exception as e:
                last_err = e
                if not _is_retryable_gemini_exception(e):
                    raise
                err_s = _gemini_error_search_text(e)
                http_code = _gemini_http_status(e)
                if ki + 1 < len(keys):
                    if http_code == 429:
                        seen_429_on_prior_key = True
                    logger.info(
                        "Gemini transient/quota error on key %s/%s; trying next API key",
                        ki + 1,
                        len(keys),
                    )
                    break
                # Last key: if we already saw 429 on an earlier key, another 429 is almost always the
                # same quota pool — do not burn minutes in sleep/backoff loops.
                if seen_429_on_prior_key and http_code == 429:
                    logger.warning(
                        "Gemini 429 on key %s/%s after a prior key also returned 429; "
                        "failing fast (same-project quota).",
                        ki + 1,
                        len(keys),
                    )
                    raise
                if _is_per_day_quota_exhausted(e):
                    logger.warning(
                        "Gemini daily quota exhausted (or per-day cap); not retrying backoff loop"
                    )
                    raise
                max_attempts = 2 if http_code == 429 else 6
                if attempt >= max_attempts - 1:
                    raise
                delay = 15.0 * (attempt + 1)
                m = re.search(r"retry in ([0-9.]+)s", err_s, re.I)
                if m:
                    delay = max(delay, float(m.group(1)) + 2.0)
                time.sleep(min(delay, 120.0))
    assert last_err is not None
    raise last_err


def fallback_sector_digest_narrative(
    constituents: list[dict[str, Any]],
    *,
    sector_id: str,
    reason: str,
) -> str:
    """
    Plain-text sector summary when Gemini is unavailable (free-tier daily cap, outage, etc.).
    Keeps audits/emails usable without LLM wording.
    """
    reason_one = (reason.strip().split("\n") or [""])[0][:420]
    lines: list[str] = [
        f"Titan V12.0 sector digest — {sector_id}",
        "Narrative unavailable (LLM offline). Snapshot from live audit metrics only.",
        f"Context: {reason_one}",
        "",
        "— Constituent snapshot —",
    ]
    for a in constituents:
        sym = str(a.get("symbol", "?"))
        ex = str(a.get("exchange", "?"))
        intent = a.get(
            "effective_intent_score",
            a.get("intent_score", a.get("equity_technical_score")),
        )
        r1 = a.get("return_1d_pct")
        z = a.get("z_score")
        parts: list[str] = [f"{sym} ({ex})"]
        if intent is not None:
            parts.append(f"intent {intent}")
        if r1 is not None:
            parts.append(f"1d {r1}%")
        if z is not None:
            parts.append(f"z {z}")
        lines.append(" · ".join(parts))
    lines.append("")
    lines.append(
        "Forensic context only; not investment advice. "
        "Raise Gemini quota (billing), switch GEMINI_MODEL, or add another API key from a different project to restore LLM narrative."
    )
    return "\n".join(lines)


def generate_sector_digest_narrative(
    constituents: list[dict[str, Any]],
    *,
    sector_id: str,
    comparison_context: dict[str, Any] | None = None,
    model_name: str | None = None,
    api_key: str | None = None,
    api_keys: Sequence[str] | None = None,
) -> str:
    """
    Single Gemini call for a whole sector (free-tier friendly: 1 request vs 1 per symbol).
    Pass compact per-stock metric dicts (e.g. output of equity audits without narrative).

    If Gemini fails (quota, outage, policy block) and GEMINI_SECTOR_DIGEST_FAIL_OPEN is true
    (default), returns a deterministic metrics snapshot instead of raising so the sector run
    can still email and persist. Set GEMINI_SECTOR_DIGEST_FAIL_OPEN=false to fail hard.
    """
    compact = [
        {
            "symbol": a.get("symbol"),
            "exchange": a.get("exchange"),
            "z_score": a.get("z_score"),
            "volume_participation_ratio": a.get("volume_participation_ratio", a.get("absorption_ratio")),
            "absorption_ratio": a.get("absorption_ratio"),
            "intent_score": a.get("intent_score"),
            "equity_technical_score": a.get("equity_technical_score", a.get("effective_intent_score")),
            "effective_intent_score": a.get("effective_intent_score"),
            "return_1d_pct": a.get("return_1d_pct"),
            "ema_200_distance_pct": a.get("ema_200_distance_pct"),
            "atr_14_pct": a.get("atr_14_pct"),
            "panic_absorption_proxy": a.get("panic_absorption_proxy"),
            "high_volume_down_day_proxy": a.get(
                "high_volume_down_day_proxy", a.get("panic_absorption_proxy")
            ),
            "trap_exit_proxy": a.get("trap_exit_proxy"),
            "event_risk_present": a.get("event_risk_present"),
            "event_risk_soon": a.get("event_risk_soon"),
            "event_days_to_next": a.get("event_days_to_next"),
            "event_types": a.get("event_types"),
            "cluster_guardrail_applied": a.get("cluster_guardrail_applied"),
            "event_guardrail_applied": a.get("event_guardrail_applied"),
            "macro_guardrail_applied": a.get("macro_guardrail_applied"),
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
    if comparison_context:
        audit_data["comparison_context"] = comparison_context
    fail_open = _env_truthy("GEMINI_SECTOR_DIGEST_FAIL_OPEN", default=True)
    try:
        return generate_titan_narrative(
            audit_data,
            model_name=model_name,
            api_key=api_key,
            api_keys=api_keys,
        )
    except (RuntimeError, ValueError) as e:
        if not fail_open:
            raise
        msg = str(e)
        if "[Gemini]" not in msg and "Policy check" not in msg:
            raise
        logger.warning(
            "Sector digest LLM failed for %r; using fallback narrative (%s)",
            sector_id,
            msg[:200],
        )
        return fallback_sector_digest_narrative(
            constituents,
            sector_id=sector_id,
            reason=msg,
        )


_PORTFOLIO_SUMMARY_USER_PREFIX = """You are summarizing a Titan portfolio-scan JSON snapshot for internal review.

Produce plain text ONLY:
• 4 to 8 lines, each beginning with '- ' (ASCII hyphen plus space).
• Lead with aggregate posture (weighted next-week vs intent scores, headline P/L if present, action bucket counts).
• Call out concentrated names (high Book %) that carry exit_risk / trim labels; mention extreme tape facts (deep single-day %) when supplied.
• If coverage_summary shows gaps (skipped symbols, invalid mappings), cite them succinctly — data may be incomplete.
• Stay factual; do not invent figures not shown in JSON.
• Titan protocol wording: forensic context only — never use Buy, Sell, Target, SL, Stop Loss or equivalent trade commands.
• Prefer neutral verbs: 'elevated risk flags', 'warrants sizing review against your mandate', 'concentrated exposure under model exit_risk'.

"""


def generate_portfolio_llm_summary(
    audit_data: dict[str, Any],
    *,
    model_name: str | None = None,
    api_key: str | None = None,
    api_keys: Sequence[str] | None = None,
) -> str:
    """
    One Gemini call: short bullet summary for portfolio email digest.
    Same compliance policy gate as narratives (forbidden wording scan + optional repair).
    """
    resolved_model = (model_name or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
    keys = _resolve_gemini_keys(api_keys, api_key)
    compact = _env_truthy("GEMINI_COMPACT_PROMPT", default=True)
    payload = sanitize_for_json(audit_data)
    json_body = (
        json.dumps(payload, default=str, separators=(",", ":"), ensure_ascii=False)
        if compact
        else json.dumps(payload, default=str, indent=2)
    )
    prompt = (
        textwrap.dedent(_PORTFOLIO_SUMMARY_USER_PREFIX).strip()
        + "\n\nAudit payload (JSON):\n"
        + json_body
        + "\n\nRespond with the bullet list only.\n"
    )

    def _gen(user_text: str) -> str:
        try:
            return _generate(keys, resolved_model, user_text)
        except Exception as e:
            raise RuntimeError(f"[Gemini] {e}") from e

    text = _gen(prompt)
    if _policy_check_passes(text):
        return text
    if not _env_truthy("GEMINI_COMPLIANCE_RETRY", default=True):
        raise ValueError(
            "[Gemini] Portfolio brief failed compliance on first draft; blocked "
            "(GEMINI_COMPLIANCE_RETRY=false, no repair call)"
        )
    repair = (
        "Your bullets failed wording policy. Rewrite keeping the same factual points "
        "using Titan forensic language only — no forbidden trade imperatives.\n\n"
        f"Rejected draft:\n{text}"
    )
    text2 = _gen(repair)
    if _policy_check_passes(text2):
        return text2
    raise ValueError("[Gemini] Portfolio brief failed compliance after retry.")


def resolve_equity_disambiguation_pick(
    user_hint: str,
    candidates: Sequence[tuple[str, str]],
    *,
    api_key: str | None = None,
    api_keys: Sequence[str] | None = None,
) -> tuple[int, float] | None:
    """
    Choose one candidate index matching free-form user_hint (research / mapping helper).
    Returns (index_into_candidates, model_confidence) or None when the model rejects all.
    """
    if not str(user_hint or "").strip() or not candidates:
        return None
    keys = _resolve_gemini_keys(api_keys, api_key)
    if not keys:
        return None
    resolved_model = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
    cata = [{"i": i, "symbol": c[0], "exchange": c[1]} for i, c in enumerate(tuple(candidates)[:54])]

    compact = _env_truthy("GEMINI_COMPACT_PROMPT", default=True)
    cat_json = json.dumps(cata, separators=(",", ":"), ensure_ascii=False) if compact else json.dumps(cata)
    prompt = (
        "Task: Match the user's search text to at most ONE listed equity entry from the catalog array.\n"
        "Respond with ONLY valid JSON: {\"pick\":null} or {\"pick\":<integer index i>,\"confidence\":number 0..1}.\n"
        "Prefer obvious company/symbol/name alignment; reject weak matches with {\"pick\":null}.\n"
        "Do not advise trading; indexing only.\n\n"
        f'User hint: {user_hint.strip()}\n\nCatalog:\n{cat_json}\n'
    )

    try:
        text = _generate(keys, resolved_model, prompt)
    except Exception:
        logger.warning("Gemini equity disambiguation call failed")
        return None
    stripped = text.strip()
    lower = stripped.lower()
    fence = "```"
    if lower.startswith(fence):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit(fence, 1)[0].strip()

    idx: int | None = None
    conf_val = 0.75
    try:
        blob = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("Gemini equity pick returned non-JSON; ignored")
        return None
    if not isinstance(blob, dict):
        return None
    if blob.get("pick") is None:
        return None
    pi = blob.get("pick")
    if isinstance(pi, bool):
        return None
    try:
        idx = int(pi)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(cata):
        return None
    raw_conf = blob.get("confidence")
    if isinstance(raw_conf, bool):
        raw_conf = None
    try:
        if raw_conf is not None:
            conf_val = max(0.0, min(1.0, float(raw_conf)))
    except (TypeError, ValueError):
        conf_val = 0.75
    if conf_val < 0.52:
        return None
    return idx, conf_val


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
    Set GEMINI_COMPLIANCE_RETRY=false to skip the second API call when the first draft fails compliance
    (saves quota; narrative may fail instead of repairing).
    """
    resolved_model = (model_name or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
    keys = _resolve_gemini_keys(api_keys, api_key)
    compact = _env_truthy("GEMINI_COMPACT_PROMPT", default=True)
    payload = sanitize_for_json(audit_data)
    json_body = (
        json.dumps(payload, default=str, separators=(",", ":"), ensure_ascii=False)
        if compact
        else json.dumps(payload, default=str, indent=2)
    )
    prompt = "Audit payload (JSON):\n" + json_body + "\n\nRespond with the post body only."

    def _gen(user_text: str) -> str:
        try:
            return _generate(keys, resolved_model, user_text)
        except Exception as e:
            raise RuntimeError(f"[Gemini] {e}") from e

    text = _gen(prompt)
    if _policy_check_passes(text):
        return text
    if not _env_truthy("GEMINI_COMPLIANCE_RETRY", default=True):
        raise ValueError(
            "[Gemini] Policy check failed on first draft; narrative blocked "
            "(GEMINI_COMPLIANCE_RETRY=false, no repair call)"
        )
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
