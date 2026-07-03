"""Map messy user search text — not exchange tickers — to listed equities before Breeze runs."""

from __future__ import annotations

import difflib
import logging
import os
import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from config_loader import load_config

if TYPE_CHECKING:
    from sector_registry import SectorInstrument

logger = logging.getLogger(__name__)

_HINT_LINE_SPLIT_TOKENS = re.compile(r"[;,]+")


def custom_symbol_llm_enabled() -> bool:
    raw = (os.environ.get("TITAN_CUSTOM_SYMBOL_LLM") or "").strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def split_custom_equity_hints(raw: str, *, max_hints: int = 120) -> list[str]:
    raw = raw.strip()
    if not raw:
        raise ValueError("custom symbol hint list is empty")
    hints: list[str] = []
    seen: set[str] = set()

    def _consume_segment(seg: str) -> None:
        t = seg.strip()
        if not t:
            return
        key = re.sub(r"\s+", " ", t).strip()
        lk = key.lower()
        if lk not in seen:
            seen.add(lk)
            hints.append(key)

    for block in raw.replace("\r", "").split("\n"):
        block = block.strip()
        if not block:
            continue
        segments = _HINT_LINE_SPLIT_TOKENS.split(block)
        if len(segments) == 1 and segments[0] == block.strip():
            _consume_segment(block)
        else:
            for seg in segments:
                _consume_segment(seg)
    if not hints:
        raise ValueError("custom symbol hint list is empty")
    if len(hints) > max_hints:
        raise ValueError(f"custom hints exceed limit ({max_hints})")
    return hints


def _slug_compact(label: str) -> str:
    return "".join(ch for ch in str(label or "").upper() if ch.isalnum())


def _tokens_from_hint(hint: str, *, min_len: int = 3) -> list[str]:
    words: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9&]{2,}", hint):
        frag = "".join(ch if ch.isalnum() or ch == "&" else "" for ch in m.group(0).upper()).strip("&")
        if len(frag) >= min_len:
            words.append(frag)
    return sorted(set(words), key=len, reverse=True)


def _build_blob_candidates(hint: str) -> list[str]:
    blobs: list[str] = []
    sc = _slug_compact(hint)
    if len(sc) >= 3:
        blobs.append(sc)
    for tok in _tokens_from_hint(hint):
        blobs.append(tok)
        st = _slug_compact(tok)
        if st != tok:
            blobs.append(st)
    out: list[str] = []
    seen: set[str] = set()
    for b in blobs:
        b2 = "".join(ch for ch in b.upper() if ch.isalnum() or ch == "&")
        if len(b2) < 3 or b2 in seen:
            continue
        seen.add(b2)
        out.append(b2)
    out.sort(key=len, reverse=True)
    return out


def _gather_scored_candidates(
    hint: str,
    *,
    uni: dict[str, dict[str, str]],
    preferred_exchange: str,
    max_symbols: int = 48,
) -> list[tuple[str, str, float]]:
    ex_pref = preferred_exchange.upper() if preferred_exchange.upper() in {"NSE", "BSE"} else "NSE"
    blobs = _build_blob_candidates(hint)
    scored: dict[tuple[str, str], float] = {}

    def bump(canonical_sym: str, exchange: str, score: float) -> None:
        k = (canonical_sym.upper(), exchange.upper())
        scored[k] = max(scored.get(k) or 0.0, score)

    for ex in (ex_pref, "BSE" if ex_pref == "NSE" else "NSE"):
        cmap = uni.get(ex, {})
        if not cmap:
            continue
        keys_plain = sorted(cmap.keys())
        keys_long = sorted(cmap.keys(), key=len, reverse=True)
        for blob in blobs:
            for mk in difflib.get_close_matches(blob, keys_plain, n=44, cutoff=0.53):
                r = SequenceMatcher(None, blob, mk).ratio()
                bump(cmap[mk], ex, max(r, 0.56))
            if len(blob) >= 4:
                for k_key in keys_long:
                    if blob in k_key or k_key in blob:
                        r = SequenceMatcher(None, blob, k_key).ratio()
                        bump(cmap[k_key], ex, max(0.86, r))

    ranked = sorted(scored.keys(), key=lambda t: scored[t], reverse=True)
    trimmed = ranked[: max(1, int(max_symbols))]
    return [(s, e, scored[(s, e)]) for s, e in trimmed]


_STRICT_EXCHANGE_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9\-&.]{0,19}$")


def is_strict_exchange_ticker(hint: str) -> bool:
    """True when ``hint`` looks like an NSE/BSE symbol (not free-form company text)."""
    compact = re.sub(r"\s+", "", str(hint or "").strip())
    if len(compact) < 2 or compact != compact.upper():
        return False
    return bool(_STRICT_EXCHANGE_TICKER_RE.match(compact))


def resolve_equity_hint(
    hint: str,
    *,
    preferred_exchange: str,
    uni: dict[str, dict[str, str]],
    cfg: Any | None,
) -> tuple[str, str, str]:
    """
    Resolve one free‑form phrase to an exchange-listed symbol Titan can request from Breeze.
    Raises ValueError when no satisfactory mapping exists.
    """
    from portfolio_analysis import _resolve_symbol

    pref = preferred_exchange.upper() if preferred_exchange.upper() in {"NSE", "BSE"} else "NSE"
    compact = re.sub(r"\s+", "", hint.strip().upper())

    if is_strict_exchange_ticker(hint):
        sym, exch, reason, conf = _resolve_symbol(compact, pref, by_exchange=uni)
        if reason != "unresolved" and conf >= 0.76:
            return sym, exch, reason
        logger.info("Custom hint exact ticker passthrough: %r → %s (%s)", hint, compact, pref)
        return compact, pref, "exact_ticker_passthrough"

    blobs = _build_blob_candidates(hint)
    best_pick: tuple[str, str, str, float] | None = None
    for blob in blobs:
        sym, exch, reason, conf = _resolve_symbol(blob, pref, by_exchange=uni)
        if reason != "unresolved" and conf >= 0.76:
            if best_pick is None or conf > best_pick[3]:
                best_pick = (sym, exch, reason, conf)
    if best_pick is not None and best_pick[3] >= 0.92:
        return best_pick[0], best_pick[1], best_pick[2]

    heur = _gather_scored_candidates(hint, uni=uni, preferred_exchange=pref)

    cfg = cfg or load_config()

    cand_pairs = [(sym, exch) for sym, exch, _sc in heur[:54]]
    if custom_symbol_llm_enabled() and cand_pairs and getattr(cfg, "gemini_api_keys", ()):
        from brain import resolve_equity_disambiguation_pick

        pick = resolve_equity_disambiguation_pick(
            hint,
            cand_pairs,
            api_keys=list(cfg.gemini_api_keys),
        )
        if pick is not None:
            idx, gc = pick
            if idx is None or gc < 0.01:
                pass
            else:
                sel = cand_pairs[idx]
                logger.info(
                    "Custom hint Gemini pick: %r → %s (%s), model_conf=%.2f",
                    hint,
                    sel[0],
                    sel[1],
                    gc,
                )
                return sel[0], sel[1], "llm_disambiguation"

    if best_pick is not None:
        logger.info(
            "Custom hint heuristic: %r → %s (%s) via %s (conf %.2f)",
            hint,
            best_pick[0],
            best_pick[1],
            best_pick[2],
            best_pick[3],
        )
        return best_pick[0], best_pick[1], best_pick[2]

    if heur:
        sym, exch, scr = heur[0]
        if scr >= 0.82:
            logger.info(
                "Custom hint string-score pick: %r → %s (%s) score=%.2f",
                hint,
                sym,
                exch,
                scr,
            )
            return sym, exch, "string_similarity_pick"

        top_txt = "; ".join(f"{s}:{e} ({r:.2f})" for s, e, r in heur[:8])
        raise ValueError(
            f"could not confidently map equity hint {hint!r}; best candidates ({top_txt}); "
            "refine wording or paste the exact exchange symbol.",
        )

    raise ValueError(f"could not map equity hint {hint!r} — no plausible candidates.")


def resolve_custom_equity_field_to_sector_instruments(
    raw: str,
    *,
    preferred_exchange: str,
    cfg: Any | None = None,
) -> tuple[list[SectorInstrument], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Produce ``SectorInstrument`` rows from raw user-entered hints (comma/semicolon/newline separated).

    Returns ``(instruments, mapping_log, skipped)`` where ``skipped`` lists hints that could not
    be resolved (reason ``unresolved``).
    """
    from portfolio_analysis import _load_active_symbol_universe
    from sector_registry import SectorInstrument

    cfg = cfg or load_config()
    uni = _load_active_symbol_universe(cfg)
    hints = split_custom_equity_hints(raw)
    out: list[SectorInstrument] = []
    mapping_log: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for h in hints:
        try:
            sym, ex, meth = resolve_equity_hint(h, preferred_exchange=preferred_exchange, uni=uni, cfg=cfg)
        except ValueError as exc:
            skipped.append({"hint": h, "symbol": None, "exchange": None, "reason": "unresolved", "error": str(exc)})
            continue
        key_pair = (sym.upper(), ex.upper())
        mapping_log.append({"hint": h, "symbol": sym, "exchange": ex, "via": meth})
        if key_pair not in seen_pairs:
            seen_pairs.add(key_pair)
            out.append(SectorInstrument(symbol=sym, exchange=ex))
    return out, mapping_log, skipped

