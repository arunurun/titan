"""Free NSE end-of-day archive access (no Breeze, no paid API).

Downloads whole-market NSE archive files (preferred over per-symbol calls) with a
browser-style cookie warm-up, retries and an on-disk cache, and exposes normalized
pandas frames. Used by the EOD ingestion scripts and the historical risk-gate backfill.

Sources (all public NSE archives):
  * sec_bhavdata_full  -> OHLC + volume + delivery qty/% (primary OHLCV + delivery feed)
  * CM UDiFF bhavcopy  -> alternative OHLC (zip)
  * ind_close_all      -> all index closes incl. "India VIX"
  * FO UDiFF bhavcopy  -> F&O futures/options OI
  * fo_secban          -> F&O ban list

No new third-party dependency: uses urllib + pandas (already required). nselib is not
used because the direct archive download (with the repo's existing cookie-warm-up
pattern) is sufficient and avoids an extra dependency.
"""

from __future__ import annotations

import io
import logging
import os
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_NSE_HOME = "https://www.nseindia.com"
_ARCH = "https://archives.nseindia.com"
_DEFAULT_TIMEOUT = 30.0


def _cache_dir() -> Path:
    raw = os.environ.get("TITAN_NSE_CACHE_DIR", "").strip()
    base = Path(raw) if raw else (Path(__file__).resolve().parents[1] / "temp" / "nse_cache")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _max_retries() -> int:
    raw = os.environ.get("TITAN_NSE_MAX_RETRIES", "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _req(url: str, referer: str = _NSE_HOME + "/") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
    )


def _download(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bytes | None, str]:
    """Cookie-warm then GET. Returns (bytes, "") on success or (None, error_tag)."""
    last_err = "unknown"
    for attempt in range(_max_retries()):
        opener = _build_opener()
        try:
            opener.open(_req(_NSE_HOME + "/"), timeout=timeout).read()
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            last_err = f"warmup_{type(exc).__name__}"
        try:
            with opener.open(_req(url), timeout=timeout) as resp:
                return resp.read(), ""
        except urllib.error.HTTPError as exc:
            last_err = f"http_{exc.code}"
            if exc.code in (403, 401):
                time.sleep(0.8 * (attempt + 1))
                continue
            if exc.code == 404:
                return None, "http_404"
            time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_err = f"err_{type(exc).__name__}"
            time.sleep(0.5 * (attempt + 1))
    return None, last_err


def _cached_bytes(url: str, cache_name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bytes | None, str]:
    cache_file = _cache_dir() / cache_name
    if cache_file.is_file() and cache_file.stat().st_size > 0:
        return cache_file.read_bytes(), "cache"
    data, err = _download(url, timeout=timeout)
    if data is not None:
        try:
            cache_file.write_bytes(data)
        except OSError:
            pass
        return data, "download"
    return None, err


def _to_dmY(d: date) -> str:
    return d.strftime("%d%m%Y")


def _to_Ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_trade_date(value: Any) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# sec_bhavdata_full: OHLC + volume + delivery (primary)
# ---------------------------------------------------------------------------

_SEC_BHAV_NUMERIC = {
    "prev_close": "PREV_CLOSE",
    "open": "OPEN_PRICE",
    "high": "HIGH_PRICE",
    "low": "LOW_PRICE",
    "last": "LAST_PRICE",
    "close": "CLOSE_PRICE",
    "avg_price": "AVG_PRICE",
    "volume": "TTL_TRD_QNTY",
    "turnover_lacs": "TURNOVER_LACS",
    "no_of_trades": "NO_OF_TRADES",
    "deliv_qty": "DELIV_QTY",
    "deliv_per": "DELIV_PER",
}


def fetch_sec_bhavdata_full(trade_date: date, *, series: tuple[str, ...] | None = ("EQ", "BE")) -> pd.DataFrame:
    """Whole-market OHLCV + delivery for one session. Empty frame on holiday/failure."""
    url = f"{_ARCH}/products/content/sec_bhavdata_full_{_to_dmY(trade_date)}.csv"
    data, src = _cached_bytes(url, f"sec_bhavdata_full_{_to_dmY(trade_date)}.csv")
    if data is None:
        if src != "http_404":
            logger.info("sec_bhavdata_full %s unavailable: %s", trade_date.isoformat(), src)
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sec_bhavdata_full parse failed %s: %s", trade_date.isoformat(), exc)
        return pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    out = pd.DataFrame()
    out["symbol"] = df.get("SYMBOL", pd.Series(dtype=str)).astype(str).str.strip().str.upper()
    out["series"] = df.get("SERIES", pd.Series(dtype=str)).astype(str).str.strip().str.upper()
    for dst, srccol in _SEC_BHAV_NUMERIC.items():
        col = df.get(srccol)
        if col is None:
            out[dst] = pd.NA
            continue
        out[dst] = pd.to_numeric(
            col.astype(str).str.strip().replace({"-": None, "": None}), errors="coerce"
        )
    out["trade_date"] = trade_date.isoformat()
    if series:
        out = out[out["series"].isin([s.upper() for s in series])]
    return out.reset_index(drop=True)


def build_ohlcv_panel(
    start: date,
    end: date,
    *,
    symbols: set[str] | None = None,
    series: tuple[str, ...] | None = ("EQ", "BE"),
    progress: bool = False,
) -> dict[str, pd.DataFrame]:
    """Per-symbol OHLCV history over [start, end] (inclusive), sorted ascending.

    Iterates calendar days, skips weekends and missing files (holidays). Each value is a
    frame with columns: trade_date(date), open, high, low, close, volume, prev_close,
    deliv_qty, deliv_per.
    """
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    want = {s.strip().upper() for s in symbols} if symbols else None
    d = start
    n_days = 0
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            frame = fetch_sec_bhavdata_full(d, series=series)
            if not frame.empty:
                n_days += 1
                if want is not None:
                    frame = frame[frame["symbol"].isin(want)]
                for rec in frame.to_dict("records"):
                    sym = rec["symbol"]
                    by_symbol.setdefault(sym, []).append(
                        {
                            "trade_date": d,
                            "open": rec.get("open"),
                            "high": rec.get("high"),
                            "low": rec.get("low"),
                            "close": rec.get("close"),
                            "volume": rec.get("volume"),
                            "prev_close": rec.get("prev_close"),
                            "deliv_qty": rec.get("deliv_qty"),
                            "deliv_per": rec.get("deliv_per"),
                        }
                    )
                if progress:
                    logger.info("bhavcopy %s rows=%d", d.isoformat(), len(frame))
        d += timedelta(days=1)
    out: dict[str, pd.DataFrame] = {}
    for sym, recs in by_symbol.items():
        frame = pd.DataFrame(recs).sort_values("trade_date").reset_index(drop=True)
        out[sym] = frame
    if progress:
        logger.info("panel built: %d trading days, %d symbols", n_days, len(out))
    return out


# ---------------------------------------------------------------------------
# India VIX (from all-index close file)
# ---------------------------------------------------------------------------


def fetch_index_close_all(trade_date: date) -> pd.DataFrame:
    url = f"{_ARCH}/content/indices/ind_close_all_{_to_dmY(trade_date)}.csv"
    data, src = _cached_bytes(url, f"ind_close_all_{_to_dmY(trade_date)}.csv")
    if data is None:
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ind_close_all parse failed %s: %s", trade_date.isoformat(), exc)
        return pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# F&O ban list
# ---------------------------------------------------------------------------


def fetch_fno_ban_list(trade_date: date | None = None) -> tuple[list[str], date | None]:
    """F&O ban list for one trade date.

    When ``trade_date`` is given, the date-stamped archive file is used so the list can be
    backfilled per session (NSE publishes one ``fo_secban_DDMMYYYY.csv`` per trading day).
    When omitted, the live single-day ``fo_secban.csv`` (next session's ban) is used.
    Empty list (and ``None`` date) on holiday/missing file/failure.
    """
    if trade_date is not None:
        url = f"{_ARCH}/archives/fo/sec_ban/fo_secban_{_to_dmY(trade_date)}.csv"
        cache_name = f"fo_secban_{_to_dmY(trade_date)}.csv"
    else:
        url = f"{_ARCH}/content/fo/fo_secban.csv"
        cache_name = "fo_secban_latest.csv"  # live file; not date-stamped
    data, _src = _cached_bytes(url, cache_name)
    if data is None:
        return [], None
    text = data.decode("utf-8", errors="replace")
    banned: list[str] = []
    ban_date: date | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("securities in ban"):
            # e.g. "Securities in Ban For Trade Date 15-JUN-2026:"
            tail = line.split("Trade Date", 1)[-1].strip().rstrip(":").strip()
            ban_date = parse_trade_date(tail)
            continue
        parts = line.split(",")
        sym = parts[-1].strip().upper()
        if sym and sym.isalnum():
            banned.append(sym)
    return banned, ban_date


# ---------------------------------------------------------------------------
# F&O UDiFF bhavcopy (futures OI / basis)
# ---------------------------------------------------------------------------


def fetch_fo_udiff_bhavcopy(trade_date: date) -> pd.DataFrame:
    url = f"{_ARCH}/content/fo/BhavCopy_NSE_FO_0_0_0_{_to_Ymd(trade_date)}_F_0000.csv.zip"
    data, src = _cached_bytes(url, f"fo_udiff_{_to_Ymd(trade_date)}.csv.zip")
    if data is None:
        return pd.DataFrame()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        inner = zf.namelist()[0]
        df = pd.read_csv(io.BytesIO(zf.read(inner)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fo_udiff parse failed %s: %s", trade_date.isoformat(), exc)
        return pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# Live JSON endpoints: FII/DII cash provisional + corporate-actions calendar
# These are not date-stamped archive files, so they are fetched fresh (no cache)
# and are best-effort (return empty on any failure; ingestion records the error).
# ---------------------------------------------------------------------------

import json as _json  # local import keeps the archive section dependency-free


def _download_json(path: str, referer: str = _NSE_HOME + "/") -> Any:
    url = path if path.startswith("http") else (_NSE_HOME + path)
    for attempt in range(_max_retries()):
        opener = _build_opener()
        try:
            opener.open(_req(_NSE_HOME + "/", referer=referer), timeout=_DEFAULT_TIMEOUT).read()
        except Exception:  # noqa: BLE001 - warm-up is best effort
            pass
        try:
            with opener.open(_req(url, referer=referer), timeout=_DEFAULT_TIMEOUT) as resp:
                return _json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            logger.info("nse json %s attempt %d failed: %s", url, attempt + 1, type(exc).__name__)
            time.sleep(0.6 * (attempt + 1))
    return None


def _num(value: Any) -> float | None:
    s = str(value if value is not None else "").replace(",", "").strip()
    if not s or s in ("-", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_fii_dii_cash() -> list[dict[str, Any]]:
    """FII/DII cash provisional (latest published session). Market-level aggregate.

    Returns a list of normalized dicts: {as_of_date, segment, fii_*, dii_*}. Empty on failure.
    """
    payload = _download_json("/api/fiidiiTradeReact")
    if not isinstance(payload, list) or not payload:
        return []
    fii: dict[str, Any] = {}
    dii: dict[str, Any] = {}
    as_of: date | None = None
    for rec in payload:
        cat = str(rec.get("category") or "").upper()
        as_of = parse_trade_date(rec.get("date")) or as_of
        if "FII" in cat or "FPI" in cat:
            fii = rec
        elif "DII" in cat:
            dii = rec
    if as_of is None:
        return []
    return [
        {
            "as_of_date": as_of.isoformat(),
            "segment": "cash",
            "fii_buy_crs": _num(fii.get("buyValue")),
            "fii_sell_crs": _num(fii.get("sellValue")),
            "fii_net_crs": _num(fii.get("netValue")),
            "dii_buy_crs": _num(dii.get("buyValue")),
            "dii_sell_crs": _num(dii.get("sellValue")),
            "dii_net_crs": _num(dii.get("netValue")),
        }
    ]


def fetch_corporate_actions(
    *, index: str = "equities", from_date: date | None = None, to_date: date | None = None
) -> list[dict[str, Any]]:
    """Corporate actions / results calendar (dividends, bonus, splits, AGM, results).

    With no dates, returns the upcoming calendar. When ``from_date``/``to_date`` are given,
    the NSE endpoint filters by ex-date, so a session range can be backfilled per date.
    """
    path = f"/api/corporates-corporateActions?index={index}"
    if from_date is not None and to_date is not None:
        path += f"&from_date={from_date.strftime('%d-%m-%Y')}&to_date={to_date.strftime('%d-%m-%Y')}"
    payload = _download_json(path)
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for rec in payload:
        sym = str(rec.get("symbol") or "").strip().upper()
        ex = parse_trade_date(rec.get("exDate"))
        purpose = str(rec.get("subject") or rec.get("purpose") or "").strip()
        if not sym or ex is None or not purpose:
            continue
        out.append(
            {
                "symbol": sym,
                "ex_date": ex.isoformat(),
                "purpose": purpose[:240],
                "series": str(rec.get("series") or "").strip() or None,
                "record_date": (parse_trade_date(rec.get("recDate")) or None)
                and parse_trade_date(rec.get("recDate")).isoformat(),
                "bc_start_date": (parse_trade_date(rec.get("bcStartDate")) or None)
                and parse_trade_date(rec.get("bcStartDate")).isoformat(),
                "bc_end_date": (parse_trade_date(rec.get("bcEndDate")) or None)
                and parse_trade_date(rec.get("bcEndDate")).isoformat(),
                "details": str(rec.get("comp") or "").strip()[:500] or None,
            }
        )
    return out
