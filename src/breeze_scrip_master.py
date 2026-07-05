"""Map NSE/BSE display symbols to Breeze ``stock_code`` (e.g. BEL → BHAELE) via ICICI scrip file.

Breeze historical API expects internal codes from the master scrip list, not always the exchange ticker.
See: https://github.com/Idirect-Tech/Breeze-Python-SDK/issues/116
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_STATIC_NSE_ALIASES: dict[str, str] | None = None

# Daily file; cache on disk to avoid downloading every symbol.
SCRIP_CSV_URL = "https://traderweb.icicidirect.com/Content/File/txtFile/ScripFile/StockScriptNew.csv"
CACHE_MAX_AGE_SEC = 86_400
_DOWNLOAD_MAX_ATTEMPTS = 3
_DOWNLOAD_RETRY_BASE_SEC = 2.0
_CACHE_LOCK = threading.Lock()
_MEMO: dict[tuple[str, str], str] | None = None
_CACHE_LOADED_AT: float = 0.0
_LOAD_FAIL_UNTIL: float = 0.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_cache_path() -> Path:
    d = _repo_root() / "data" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "StockScriptNew.csv"


def _static_nse_aliases_path() -> Path:
    return _repo_root() / "data" / "breeze_nse_aliases.json"


def _load_static_nse_aliases() -> dict[str, str]:
    """Committed NSE→Breeze fallbacks when ICICI scrip download is unavailable."""
    global _STATIC_NSE_ALIASES
    if _STATIC_NSE_ALIASES is not None:
        return _STATIC_NSE_ALIASES
    path = _static_nse_aliases_path()
    aliases: dict[str, str] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                aliases = {
                    str(k).strip().upper(): str(v).strip().upper()
                    for k, v in raw.items()
                    if str(k).strip() and str(v).strip()
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Could not read %s: %s", path, e)
    _STATIC_NSE_ALIASES = aliases
    return aliases


def _load_cache_bytes(path: Path, *, max_age_sec: float | None = CACHE_MAX_AGE_SEC) -> bytes | None:
    if not path.is_file():
        return None
    if max_age_sec is not None and time.time() - path.stat().st_mtime > max_age_sec:
        return None
    return path.read_bytes()


def _download_scrip_csv_bytes() -> bytes:
    req = urllib.request.Request(
        SCRIP_CSV_URL,
        headers={"User-Agent": "Titan/1.0 (breeze_scrip_master)"},
    )
    last_err: Exception | None = None
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = exc
            if attempt >= _DOWNLOAD_MAX_ATTEMPTS:
                break
            delay = _DOWNLOAD_RETRY_BASE_SEC * (2 ** (attempt - 1))
            logger.info(
                "Scrip master download attempt %s/%s failed (%s); retrying in %.1fs",
                attempt,
                _DOWNLOAD_MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def _fetch_scrip_csv(cache_path: Path) -> str:
    cached = _load_cache_bytes(cache_path)
    if cached is not None:
        return cached.decode("utf-8", errors="replace")
    logger.info("Downloading Breeze scrip master from ICICI (StockScriptNew.csv)...")
    try:
        data = _download_scrip_csv_bytes()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        stale = _load_cache_bytes(cache_path, max_age_sec=None)
        if stale is not None:
            age_hours = (time.time() - cache_path.stat().st_mtime) / 3600.0
            logger.warning(
                "Could not refresh scrip master: %s — using stale cached copy (age=%.1fh).",
                exc,
                age_hours,
            )
            return stale.decode("utf-8", errors="replace")
        raise
    cache_path.write_bytes(data)
    return data.decode("utf-8", errors="replace")


def _build_lookup(text: str) -> dict[tuple[str, str], str]:
    """
    ICICI CSV columns include: SC (Breeze code), EC (NSE/BSE), NS (symbol on that exchange).
    Match on (EC.upper(), NS.strip().upper()) -> SC.
    """
    out: dict[tuple[str, str], str] = {}
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "SC" not in reader.fieldnames:
        logger.warning("Scrip CSV missing expected headers; Breeze code resolution disabled.")
        return out
    ec_key = "EC"
    ns_key = "NS"
    sc_key = "SC"
    for row in reader:
        try:
            ec = (row.get(ec_key) or "").strip().upper()
            ns = (row.get(ns_key) or "").strip().upper()
            sc = (row.get(sc_key) or "").strip().upper()
        except (AttributeError, TypeError):
            continue
        if not ec or not ns or not sc:
            continue
        if ec not in ("NSE", "BSE"):
            continue
        # First wins; duplicates rare for (EC, NS)
        out.setdefault((ec, ns), sc)
    logger.info("Breeze scrip lookup built: %s (EC,NS) -> SC entries", len(out))
    return out


def _ensure_lookup() -> dict[tuple[str, str], str]:
    global _MEMO, _CACHE_LOADED_AT, _LOAD_FAIL_UNTIL
    with _CACHE_LOCK:
        now = time.time()
        if now < _LOAD_FAIL_UNTIL:
            return {}
        if _MEMO is not None and (now - _CACHE_LOADED_AT) < 3600:
            return _MEMO
        path = _default_cache_path()
        try:
            text = _fetch_scrip_csv(path)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            static = _load_static_nse_aliases()
            logger.warning(
                "Could not refresh scrip master: %s — using %s committed NSE aliases as fallback.",
                e,
                len(static),
            )
            _LOAD_FAIL_UNTIL = now + 300.0
            return {}
        _MEMO = _build_lookup(text)
        _CACHE_LOADED_AT = now
        _LOAD_FAIL_UNTIL = 0.0
        return _MEMO


def resolve_breeze_stock_code(symbol: str, exchange_code: str) -> str:
    """
    Return Breeze ``stock_code`` for historical/quote APIs.

    If unknown (e.g. offline, new listing), returns ``symbol`` uppercased.
    """
    sym = symbol.strip().upper()
    ex = exchange_code.strip().upper()
    if ex not in ("NSE", "BSE"):
        return sym
    if sym == "NIFTY" and ex == "NSE":
        return "NIFTY"
    table = _ensure_lookup()
    key = (ex, sym)
    if key in table:
        return table[key]
    if ex == "NSE":
        static = _load_static_nse_aliases()
        if sym in static:
            return static[sym]
    return sym


def clear_scrip_cache_for_tests() -> None:
    """Reset in-memory map (tests only)."""
    global _MEMO, _CACHE_LOADED_AT, _LOAD_FAIL_UNTIL, _STATIC_NSE_ALIASES
    with _CACHE_LOCK:
        _MEMO = None
        _CACHE_LOADED_AT = 0.0
        _LOAD_FAIL_UNTIL = 0.0
        _STATIC_NSE_ALIASES = None
