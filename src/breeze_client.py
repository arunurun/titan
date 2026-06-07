"""ICICI Breeze market data fetch with retries."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import pandas as pd
from breeze_connect import BreezeConnect
from zoneinfo import ZoneInfo

from breeze_scrip_master import resolve_breeze_stock_code

IST = ZoneInfo("Asia/Kolkata")


class _BreezeCredentials(Protocol):
    breeze_api_key: str
    breeze_secret: str
    breeze_session_token: str


logger = logging.getLogger(__name__)
# Reduce verbose SDK payload dumps in workflow logs.
logging.getLogger("APILogger").setLevel(logging.INFO)
_HIST_CALL_LOCK = threading.Lock()
_LAST_HIST_CALL_AT = 0.0
_MIN_HIST_CALL_INTERVAL_SECONDS = 0.75
_HISTORICAL_CALL_TIMEOUT_SECONDS_DEFAULT = 25.0


class BreezeHistoricalTimeoutError(RuntimeError):
    """Raised when Breeze historical data call exceeds hard timeout."""


def _reconcile_mode_enabled() -> bool:
    return (os.environ.get("TITAN_RECONCILE_MODE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _ensure_breeze_allowed(action: str) -> None:
    if _reconcile_mode_enabled():
        raise RuntimeError(
            f"[ReconcileGuard] Breeze market fetch blocked in reconcile mode ({action}). "
            "Use the dedicated Supabase-only reconcile runner."
        )


def _min_hist_call_interval_seconds() -> float:
    raw = os.environ.get("BREEZE_HIST_CALL_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _MIN_HIST_CALL_INTERVAL_SECONDS
    try:
        v = float(raw)
    except ValueError:
        return _MIN_HIST_CALL_INTERVAL_SECONDS
    return max(0.0, v)


def _historical_call_timeout_seconds() -> float:
    raw = os.environ.get("BREEZE_HISTORICAL_CALL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _HISTORICAL_CALL_TIMEOUT_SECONDS_DEFAULT
    try:
        v = float(raw)
    except ValueError:
        return _HISTORICAL_CALL_TIMEOUT_SECONDS_DEFAULT
    return max(0.0, v)


def _call_historical_with_timeout(
    breeze: BreezeConnect,
    *,
    timeout_seconds: float,
    kwargs: dict[str, Any],
) -> Any:
    if timeout_seconds <= 0.0:
        return breeze.get_historical_data(**kwargs)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _invoke() -> None:
        try:
            result["value"] = breeze.get_historical_data(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            error["exc"] = exc

    # Daemon thread prevents a hung SDK call from blocking process shutdown.
    t = threading.Thread(target=_invoke, daemon=True)
    t.start()
    t.join(timeout_seconds)
    if t.is_alive():
        raise BreezeHistoricalTimeoutError(
            f"[Breeze] Historical data call timed out after {timeout_seconds:.1f}s"
        )
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


def _reserve_breeze_call_slot() -> None:
    global _LAST_HIST_CALL_AT
    interval = _min_hist_call_interval_seconds()
    wait = 0.0
    with _HIST_CALL_LOCK:
        now = time.monotonic()
        earliest = _LAST_HIST_CALL_AT + interval
        scheduled = max(now, earliest)
        wait = max(0.0, scheduled - now)
        _LAST_HIST_CALL_AT = scheduled
    if wait > 0:
        time.sleep(wait)


def _rate_limited_historical_call(breeze: BreezeConnect, **kwargs: Any) -> Any:
    _reserve_breeze_call_slot()
    return _call_historical_with_timeout(
        breeze,
        timeout_seconds=_historical_call_timeout_seconds(),
        kwargs=kwargs,
    )


def _rate_limited_quote_call(breeze: BreezeConnect, **kwargs: Any) -> Any:
    _reserve_breeze_call_slot()
    return breeze.get_quotes(**kwargs)


def create_breeze_session(config: _BreezeCredentials) -> BreezeConnect:
    """Create a Breeze client and authenticate (reuse for multiple API calls in one run)."""
    _ensure_breeze_allowed("create_breeze_session")
    breeze = BreezeConnect(api_key=config.breeze_api_key)
    try:
        breeze.generate_session(
            api_secret=config.breeze_secret,
            session_token=config.breeze_session_token,
        )
    except Exception as e:
        msg = str(e).lower()
        if "session" in msg and ("expired" in msg or "expire" in msg):
            raise RuntimeError(
                "[Breeze] Session token expired. Get a new session from ICICI (browser login), then update: "
                "local .env BREEZE_SESSION_TOKEN, GitHub secret BREEZE_SESSION_TOKEN, and/or Supabase session_config. "
                "Run: python scripts/breeze_session.py"
            ) from e
        raise
    return breeze


def _expiry_iso_for_calendar_date(d: date) -> str:
    """Breeze option-chain docs use YYYY-MM-DDT06:00:00.000Z (not IST-midnight UTC)."""
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T06:00:00.000Z"


def iter_weekly_expiry_candidates(
    weeks_ahead: int = 8,
    reference: datetime | None = None,
) -> list[str]:
    """
    Upcoming NIFTY weekly expiries as Breeze expiry strings.
    NSE moved NIFTY weekly expiry to Tuesday from Sep 2025 (was Thursday).
    """
    ref = reference or datetime.now(IST)
    d0 = ref.date()
    # Tuesday == 1 (Monday=0). Include same-day if today is Tuesday.
    days_to_tue = (1 - d0.weekday()) % 7
    first_tue = d0 + timedelta(days=days_to_tue)
    return [_expiry_iso_for_calendar_date(first_tue + timedelta(days=7 * i)) for i in range(weeks_ahead)]


def _last_tuesday_of_month(year: int, month: int) -> date:
    """Last Tuesday of a calendar month (NSE monthly F&O expiry since Sep 2025)."""
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def iter_monthly_expiry_candidates(
    months_ahead: int = 4,
    reference: datetime | None = None,
) -> list[str]:
    """Upcoming monthly stock/index F&O expiries (last Tuesday of month)."""
    ref = reference or datetime.now(IST)
    year, month = ref.year, ref.month
    out: list[str] = []
    for _ in range(months_ahead):
        exp = _last_tuesday_of_month(year, month)
        if exp >= ref.date():
            out.append(_expiry_iso_for_calendar_date(exp))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


_INDEX_WEEKLY_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"})


def expiry_candidates_for_underlying(
    stock_code: str,
    *,
    max_tries: int = 8,
) -> list[str]:
    """Weekly expiries for index options; monthly for single-stock F&O."""
    code = str(stock_code or "").strip().upper()
    if code in _INDEX_WEEKLY_UNDERLYINGS:
        return iter_weekly_expiry_candidates(weeks_ahead=max_tries)
    return iter_monthly_expiry_candidates(months_ahead=max_tries)


_OPTION_CHAIN_LOCK = threading.Lock()
_LAST_OPTION_CHAIN_CALL_AT = 0.0
_MIN_OPTION_CHAIN_INTERVAL_SECONDS = 0.5


def _throttle_option_chain_call() -> None:
    global _LAST_OPTION_CHAIN_CALL_AT
    interval = _min_hist_call_interval_seconds()
    with _OPTION_CHAIN_LOCK:
        now = time.monotonic()
        wait = interval - (now - _LAST_OPTION_CHAIN_CALL_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_OPTION_CHAIN_CALL_AT = time.monotonic()


def _is_breeze_no_data_response(raw: dict[str, Any]) -> bool:
    err = raw.get("Error")
    if err is None:
        return False
    msg = str(err).lower()
    return ("no data" in msg) or ("historical data fail" in msg)


def _is_breeze_rate_limit_response(raw: dict[str, Any]) -> bool:
    err = str(raw.get("Error", "")).lower()
    return ("limit exceed" in err) or ("api call per minute" in err)


def _rows_from_option_response(raw: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"[Breeze] {label}: unexpected Breeze response: {raw!r}")
    if raw.get("Success") is None:
        # Breeze returns HTTP 200 with Success=None, Error='No Data Found' when chain is empty / unknown expiry.
        if _is_breeze_no_data_response(raw):
            logger.info("%s: no chain data (%s)", label, raw.get("Error"))
            return []
        raise RuntimeError(f"[Breeze] {label}: unexpected Breeze response: {raw!r}")
    rows = raw["Success"]
    if not isinstance(rows, list):
        raise RuntimeError(f"[Breeze] {label}: Success is not a list: {raw!r}")
    return [r for r in rows if isinstance(r, dict)]


def _oi_from_row(row: dict[str, Any]) -> float:
    v = row.get("open_interest")
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _strike_from_row(row: dict[str, Any]) -> float | None:
    sp = row.get("strike_price")
    if sp is None or sp == "":
        return None
    try:
        return float(sp)
    except (TypeError, ValueError):
        return None


def fetch_option_metrics_for_underlying(
    breeze: BreezeConnect,
    stock_code: str,
    expiry_date_iso: str,
) -> dict[str, Any]:
    """
    Total call/put open interest and strike×OI frames for an F&O underlying.
    Two requests: full call side and full put side (Breeze requires explicit right when expiry is set).
    """
    _ensure_breeze_allowed("fetch_option_metrics_for_underlying")
    code = str(stock_code or "").strip().upper()
    _throttle_option_chain_call()
    common = dict(
        stock_code=code,
        exchange_code="NFO",
        expiry_date=expiry_date_iso,
        product_type="options",
        strike_price="",
    )
    raw_calls = breeze.get_option_chain_quotes(right="call", **common)
    raw_puts = breeze.get_option_chain_quotes(right="put", **common)
    call_rows = _rows_from_option_response(raw_calls, f"option chain calls ({code})")
    put_rows = _rows_from_option_response(raw_puts, f"option chain puts ({code})")
    total_call_oi = sum(_oi_from_row(r) for r in call_rows)
    total_put_oi = sum(_oi_from_row(r) for r in put_rows)

    def _rows_to_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
        by_strike: dict[float, float] = {}
        for r in rows:
            sk = _strike_from_row(r)
            if sk is None:
                continue
            by_strike[sk] = by_strike.get(sk, 0.0) + _oi_from_row(r)
        return pd.DataFrame(
            [{"strike": k, "oi": v} for k, v in sorted(by_strike.items())],
            columns=["strike", "oi"],
        )

    call_chain_df = _rows_to_df(call_rows)
    put_chain_df = _rows_to_df(put_rows)
    by_strike: dict[float, float] = {}
    for r in call_rows + put_rows:
        sk = _strike_from_row(r)
        if sk is None:
            continue
        by_strike[sk] = by_strike.get(sk, 0.0) + _oi_from_row(r)
    chain_df = pd.DataFrame(
        [{"strike": k, "oi": v} for k, v in sorted(by_strike.items())],
        columns=["strike", "oi"],
    )
    return {
        "underlying": code,
        "call_oi": total_call_oi,
        "put_oi": total_put_oi,
        "chain_df": chain_df,
        "call_chain_df": call_chain_df,
        "put_chain_df": put_chain_df,
        "expiry_date": expiry_date_iso,
    }


def fetch_nifty_option_metrics(
    breeze: BreezeConnect,
    expiry_date_iso: str,
) -> dict[str, Any]:
    """NIFTY wrapper around :func:`fetch_option_metrics_for_underlying`."""
    return fetch_option_metrics_for_underlying(breeze, "NIFTY", expiry_date_iso)


def _empty_option_metrics_payload(
    *,
    underlying: str,
    tried: list[str],
) -> dict[str, Any]:
    return {
        "underlying": underlying,
        "call_oi": 0.0,
        "put_oi": 0.0,
        "chain_df": pd.DataFrame(columns=["strike", "oi"]),
        "call_chain_df": pd.DataFrame(columns=["strike", "oi"]),
        "put_chain_df": pd.DataFrame(columns=["strike", "oi"]),
        "expiry_date": None,
        "option_chain_unavailable": True,
        "expiry_try_index": None,
        "expiry_tries": tried,
        "fallback_used": len(tried) > 1,
    }


def fetch_option_metrics_with_expiry_fallback(
    breeze: BreezeConnect,
    stock_code: str,
    *,
    max_expiry_tries: int = 8,
) -> dict[str, Any]:
    """Try successive expiries until a chain returns non-zero OI depth."""
    code = str(stock_code or "").strip().upper()
    tried: list[str] = []
    for i, expiry in enumerate(
        expiry_candidates_for_underlying(code, max_tries=max_expiry_tries),
        start=1,
    ):
        tried.append(expiry)
        try:
            m = fetch_option_metrics_for_underlying(breeze, code, expiry)
            if m["call_oi"] == 0.0 and m["put_oi"] == 0.0:
                logger.warning(
                    "Option chain has zero OI for %s expiry %s; trying next.",
                    code,
                    expiry,
                )
                continue
            m["expiry_try_index"] = i
            m["expiry_tries"] = tried
            m["fallback_used"] = i > 1
            return m
        except Exception as e:
            logger.warning("Option chain fetch failed for %s expiry %s: %s", code, expiry, e)
    logger.error(
        "%s option chain unavailable after %s expiry tries; continuing without OI/PCR.",
        code,
        max_expiry_tries,
    )
    return _empty_option_metrics_payload(underlying=code, tried=tried)


def fetch_nifty_option_metrics_with_expiry_fallback(
    breeze: BreezeConnect,
    *,
    max_expiry_tries: int = 8,
) -> dict[str, Any]:
    """NIFTY wrapper around :func:`fetch_option_metrics_with_expiry_fallback`."""
    return fetch_option_metrics_with_expiry_fallback(breeze, "NIFTY", max_expiry_tries=max_expiry_tries)


def volume_participation_ratio(ohlc_df: pd.DataFrame) -> float:
    """
    Cash-market **volume participation**: last session volume / trailing average volume.

    This is **not** delivery-based absorption and **not** FII/DII flow. It measures
    whether today's turnover is high vs recent sessions (participation proxy).
    """
    if ohlc_df.empty or "volume" not in ohlc_df.columns:
        return float("nan")
    v = pd.to_numeric(ohlc_df["volume"], errors="coerce").dropna()
    if len(v) < 2:
        return float("nan")
    current = float(v.iloc[-1])
    prior = v.iloc[:-1]
    tail = prior.tail(5)
    avg = float(tail.mean()) if not tail.empty else float(prior.mean())
    if avg == 0.0:
        return float("inf") if current > 0 else 0.0
    return current / avg


def volume_absorption_ratio(ohlc_df: pd.DataFrame) -> float:
    """Deprecated alias for :func:`volume_participation_ratio` (legacy name)."""
    return volume_participation_ratio(ohlc_df)


def fetch_equity_data(
    config: _BreezeCredentials,
    stock_code: str,
    exchange_code: str,
    *,
    breeze: BreezeConnect | None = None,
    lookback_calendar_days: int = 60,
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
    allow_exchange_fallback: bool = True,
) -> pd.DataFrame:
    """
    Fetch cash OHLC (and volume) for an equity symbol on NSE or BSE.
    Retries up to `max_retries` times with exponential backoff on failure.
    """
    _ensure_breeze_allowed("fetch_equity_data")
    breeze = breeze or create_breeze_session(config)
    sc_raw = stock_code.strip().upper()
    ex = exchange_code.strip().upper()
    if ex not in ("NSE", "BSE"):
        raise ValueError(f"exchange_code must be NSE or BSE, got {exchange_code!r}")

    # Breeze expects ICICI scrip codes (e.g. BHAELE), not always NSE tickers (e.g. BEL).
    sc = resolve_breeze_stock_code(sc_raw, ex)
    if sc != sc_raw:
        logger.info("Breeze stock_code resolved %s (%s) -> %s", sc_raw, ex, sc)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_calendar_days)
    from_date = start.strftime("%Y-%m-%dT00:00:00.000Z")
    to_date = end.strftime("%Y-%m-%dT23:59:59.999Z")

    def _fetch_one_exchange(exchange: str) -> pd.DataFrame:
        stock_code = resolve_breeze_stock_code(sc_raw, exchange)
        label = f"{sc_raw}->{stock_code} ({exchange})" if stock_code != sc_raw else f"{stock_code} ({exchange})"
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                raw = _rate_limited_historical_call(
                    breeze,
                    interval="1day",
                    from_date=from_date,
                    to_date=to_date,
                    stock_code=stock_code,
                    exchange_code=exchange,
                    product_type="cash",
                )
                if not isinstance(raw, dict):
                    raise RuntimeError(f"[Breeze] Unexpected historical response: {raw!r}")
                if raw.get("Success") is None:
                    if _is_breeze_rate_limit_response(raw):
                        raise RuntimeError(f"[Breeze] Rate limited: {raw.get('Error')}")
                    # Breeze sometimes returns HTTP 200 with Error='No Data Found' for valid but inactive/unavailable scrips.
                    # Treat this as a clean no-data skip so sector runs stay resilient.
                    if _is_breeze_no_data_response(raw):
                        logger.info("Breeze historical no data for %s: %s", label, raw.get("Error"))
                        return pd.DataFrame()
                    raise RuntimeError(f"[Breeze] Unexpected historical response: {raw!r}")
                rows = raw["Success"]
                if not rows:
                    return pd.DataFrame()
                return pd.DataFrame(rows)
            except Exception as e:
                if isinstance(e, BreezeHistoricalTimeoutError):
                    raise RuntimeError(f"[Breeze] {label} historical fetch timeout") from e
                last_err = e
                logger.warning("Breeze fetch attempt %s for %s failed: %s", attempt + 1, label, e)
                if attempt < max_retries:
                    msg = str(e).lower()
                    if "rate limited" in msg or "limit exceed" in msg:
                        # Breeze minute caps need much longer cool-off than standard network retries.
                        sleep_seconds = max(20.0, 20.0 * (attempt + 1))
                    else:
                        sleep_seconds = backoff_base_seconds * (2**attempt)
                    time.sleep(sleep_seconds)
        raise RuntimeError(f"[Breeze] {label} historical fetch failed after retries") from last_err

    primary_df = _fetch_one_exchange(ex)
    primary_df.attrs["exchange_requested"] = ex
    primary_df.attrs["exchange_used"] = ex
    primary_df.attrs["exchange_fallback_used"] = False
    if not primary_df.empty or not allow_exchange_fallback:
        return primary_df

    alt = "BSE" if ex == "NSE" else "NSE"
    logger.info(
        "Breeze returned no rows for %s (%s); trying fallback exchange %s",
        sc_raw,
        ex,
        alt,
    )
    fallback_df = _fetch_one_exchange(alt)
    fallback_df.attrs["exchange_requested"] = ex
    fallback_df.attrs["exchange_used"] = alt
    fallback_df.attrs["exchange_fallback_used"] = True
    return fallback_df


def _normalize_quote_number(val: Any) -> float:
    if val is None or val == "":
        return float("nan")
    try:
        v = float(val)
    except (TypeError, ValueError):
        return float("nan")
    return v if not math.isnan(v) else float("nan")


def _pick_quote_row(rows: list[Any]) -> dict[str, Any] | None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        ex = str(row.get("exchange_code", "")).strip().upper()
        if ex in ("", "NA"):
            continue
        ltp = _normalize_quote_number(row.get("ltp"))
        if not math.isnan(ltp) and ltp > 0.0:
            return row
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def fetch_equity_quote(
    config: _BreezeCredentials,
    stock_code: str,
    exchange_code: str,
    *,
    breeze: BreezeConnect | None = None,
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
) -> dict[str, Any]:
    """
    Live cash quote for an equity symbol. Returns normalized floats where possible.
    """
    _ensure_breeze_allowed("fetch_equity_quote")
    breeze = breeze or create_breeze_session(config)
    sc_raw = stock_code.strip().upper()
    ex = exchange_code.strip().upper()
    if ex not in ("NSE", "BSE"):
        raise ValueError(f"exchange_code must be NSE or BSE, got {exchange_code!r}")

    sc = resolve_breeze_stock_code(sc_raw, ex)
    if sc != sc_raw:
        logger.info("Breeze quote stock_code resolved %s (%s) -> %s", sc_raw, ex, sc)

    label = f"{sc_raw}->{sc} ({ex})" if sc != sc_raw else f"{sc} ({ex})"
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = _rate_limited_quote_call(
                breeze,
                stock_code=sc,
                exchange_code=ex,
                expiry_date="",
                product_type="cash",
                right="",
                strike_price="",
            )
            if not isinstance(raw, dict):
                raise RuntimeError(f"[Breeze] Unexpected quote response: {raw!r}")
            if raw.get("Success") is None:
                if _is_breeze_rate_limit_response(raw):
                    raise RuntimeError(f"[Breeze] Rate limited: {raw.get('Error')}")
                if _is_breeze_no_data_response(raw):
                    logger.info("Breeze quote no data for %s: %s", label, raw.get("Error"))
                    return {}
                raise RuntimeError(f"[Breeze] Unexpected quote response: {raw!r}")
            rows = raw["Success"]
            if not isinstance(rows, list) or not rows:
                return {}
            row = _pick_quote_row(rows)
            if not row:
                return {}
            ltp = _normalize_quote_number(row.get("ltp"))
            previous_close = _normalize_quote_number(row.get("previous_close"))
            ltp_pct = _normalize_quote_number(row.get("ltp_percent_change"))
            return {
                "ltp": ltp,
                "previous_close": previous_close,
                "ltp_percent_change": ltp_pct,
                "ltt": row.get("ltt"),
                "open": _normalize_quote_number(row.get("open")),
                "high": _normalize_quote_number(row.get("high")),
                "low": _normalize_quote_number(row.get("low")),
            }
        except Exception as e:
            last_err = e
            logger.warning("Breeze quote attempt %s for %s failed: %s", attempt + 1, label, e)
            if attempt < max_retries:
                msg = str(e).lower()
                if "rate limited" in msg or "limit exceed" in msg:
                    sleep_seconds = max(20.0, 20.0 * (attempt + 1))
                else:
                    sleep_seconds = backoff_base_seconds * (2**attempt)
                time.sleep(sleep_seconds)
    raise RuntimeError(f"[Breeze] {label} quote fetch failed after retries") from last_err


def fetch_nifty_data(
    config: _BreezeCredentials,
    *,
    breeze: BreezeConnect | None = None,
    lookback_calendar_days: int = 60,
    max_retries: int = 3,
    backoff_base_seconds: float = 2.0,
) -> pd.DataFrame:
    """
    Fetch NIFTY cash OHLC (and volume) from Breeze API.
    Longer lookback supports z-score window and 5d volume averaging.
    Retries up to `max_retries` times with exponential backoff on failure.
    Raises RuntimeError if all attempts fail (caller may mark task BLOCKED).
    """
    _ensure_breeze_allowed("fetch_nifty_data")
    breeze = breeze or create_breeze_session(config)
    return fetch_equity_data(
        config,
        "NIFTY",
        "NSE",
        breeze=breeze,
        lookback_calendar_days=lookback_calendar_days,
        max_retries=max_retries,
        backoff_base_seconds=backoff_base_seconds,
    )
