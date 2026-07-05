"""Small & micro-cap breakout scanner for Titan control UI and CLI."""

from __future__ import annotations

import csv
import datetime
import json
import logging
import os
import random
import time
import traceback
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .breakout_evidence import (
        base_accumulation_pass,
        composite_rank_score,
        compute_evidence_metrics,
        persistence_pass_min,
        relative_strength_vs_benchmark,
        return_5d_pct,
        _atr_simple,
    )
    from .breakout_breeze_codes import build_breeze_code_map
    from .breakout_setup import (
        SETUP_CAP_PER_TIER,
        SIGNAL_TIER_PRE_BREAKOUT,
        evaluate_setup_as_of,
    )
    from .breakout_store import (
        build_analysis_record,
        persist_breakout_stock_analysis,
    )
    from .config_loader import load_config
except ImportError:
    from breakout_evidence import (
        base_accumulation_pass,
        composite_rank_score,
        compute_evidence_metrics,
        persistence_pass_min,
        relative_strength_vs_benchmark,
        return_5d_pct,
        _atr_simple,
    )
    from breakout_breeze_codes import build_breeze_code_map
    from breakout_setup import (
        SETUP_CAP_PER_TIER,
        SIGNAL_TIER_PRE_BREAKOUT,
        evaluate_setup_as_of,
    )
    from breakout_store import (
        build_analysis_record,
        persist_breakout_stock_analysis,
    )
    from config_loader import load_config

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Alternate-pass thresholds (see evaluate_bars_as_of)
PCT_CHANGE_MIN = 3.0
PCT_CHANGE_MAX_NORMAL = 12.0
PCT_CHANGE_MAX_POWER_GAP = 20.0
ADX_SOFT_FLOOR = 20.0
ADX_HARD_FLOOR = 25.0
RSI_MIN = 50.0
RSI_MAX_NORMAL = 70.0
HOT_VOL_THRESHOLD = 5.0
SMA20_RECLAIM_VOL_THRESHOLD = 5.0
MICRO_CAP_VOL_CONTINUATION = 2.5
VOL_CUM_DAYS = 3

# Pre-signal validation (T-15..T-1 history before signal day T)
PRE_SIGNAL_FULL_LOOKBACK = 15
PRE_SIGNAL_CUM_RETURN_LOOKBACK = 10
PRE_SIGNAL_CUM_RETURN_MAX = 30.0
PRE_SIGNAL_VOL_SPIKE_MULT = 2.0
PRE_SIGNAL_VOL_SPIKE_DAYS_MAX = 4
PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK = 10
PRE_SIGNAL_ADX_SOFT_SHORT_LOOKBACK = 5
PRE_SIGNAL_COOLDOWN_SESSIONS = 10
PRE_SIGNAL_COOLDOWN_CONSOLIDATION_MAX = 12.0
PRE_SIGNAL_COOLDOWN_DIST_20D_HIGH_MIN = -3.0
UPPER_CIRCUIT_PCT_MIN = 4.9
MARKET_REGIME_BENCHMARK = "NIFTY_SMALLCAP_100.NS"
MARKET_REGIME_SMA_WINDOW = 20
CLOSE_POSITION_MIN = 0.5
IMMINENT_EARNINGS_DAYS = 3
ADX_SOFT_VOL_BONUS = 0.5
ADX_SOFT_CUM_RETURN_MAX = 20.0
POWER_GAP_CUM_RETURN_MAX = 15.0
STANDARD_ADX_TRAJECTORY_VOL_EXCEPTION = 7.0
POWER_GAP_VOL_RECOVERY_THRESHOLD = 5.5
SIGNAL_TIER_PASS = "PASS"
SIGNAL_TIER_WATCH = "WATCH"
SIGNAL_TIER_SETUP = SIGNAL_TIER_PRE_BREAKOUT

_BREAKOUT_STAGE_LABELS: dict[int, str] = {
    1: "Stage 1 Fresh",
    2: "Stage 2 Young",
    3: "Stage 3 Parabolic",
}

# URLs for official NSE index constituent lists
INDEX_URLS = {
    "SMALL_CAP_100": "https://archives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
    "MICRO_CAP_250": "https://www.niftyindices.com/IndexConstituent/ind_niftymicrocap250_list.csv"
}

# Graded Technical Filters optimized for Small & Micro Caps
FILTERS = {
    "SMALL_CAP_100": {"vol_mult": 3.5, "min_price": 15.0, "type": "Small-Cap (Nifty Smallcap 100)"},
    "MICRO_CAP_250": {"vol_mult": 3.0, "min_price": 10.0, "type": "Micro-Cap (Nifty Microcap 250)"}
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.yahoo.com/',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

YAHOO_429_BACKOFF_SEC = (60, 120, 180)
YAHOO_COOKIE_HEADER = None
YAHOO_CACHE_DIR: str | None = None
_OUTPUT_DIR: Path | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_output_dir() -> Path:
    return _repo_root() / "output" / "breakouts"


def resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    if output_dir is not None:
        path = Path(output_dir)
    else:
        env = os.environ.get("BREAKOUT_OUTPUT_DIR", "").strip()
        path = Path(env) if env else default_output_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _yahoo_cache_dir() -> str:
    global YAHOO_CACHE_DIR
    if YAHOO_CACHE_DIR is None:
        cache_path = _repo_root() / "data" / "cache" / "breakout_yahoo"
        YAHOO_CACHE_DIR = str(cache_path)
    os.makedirs(YAHOO_CACHE_DIR, exist_ok=True)
    return YAHOO_CACHE_DIR


def _yahoo_cache_path(ticker):
    safe = ticker.replace("/", "_").replace("\\", "_")
    day = datetime.date.today().strftime("%Y%m%d")
    return os.path.join(_yahoo_cache_dir(), f"{safe}_{day}.json")


def warm_yahoo_session():
    global YAHOO_COOKIE_HEADER
    import http.cookiejar

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request("https://finance.yahoo.com/", headers=HEADERS)
    try:
        with opener.open(req, timeout=30) as response:
            response.read()
        YAHOO_COOKIE_HEADER = "; ".join(f"{c.name}={c.value}" for c in cj)
        print("Yahoo session warm-up OK (cookies captured).", flush=True)
    except Exception as e:
        YAHOO_COOKIE_HEADER = None
        print(f"Yahoo session warm-up failed ({type(e).__name__}); continuing without cookies.", flush=True)


def _yahoo_pre_request_sleep():
    time.sleep(random.uniform(2.5, 3.0) + random.uniform(0.0, 1.5))


# NSE symbols that were mangled (e.g. & stripped) before Yahoo fetch.
_NSE_YAHOO_SYMBOL_ALIASES: dict[str, str] = {
    "GMRPUI": "GMRP&UI",  # GMR Power and Urban Infra (Nifty Microcap 250)
}


def _normalize_nse_symbol(symbol):
    """Keep NSE symbol as-is; decode %26 to & only if CSV has URL-encoded form."""
    decoded = symbol.replace("%26", "&")
    return _NSE_YAHOO_SYMBOL_ALIASES.get(decoded, decoded)


def _resolve_yahoo_ticker(ticker: str) -> str:
    """Apply NSE symbol normalization to a Yahoo ticker (SYMBOL.NS)."""
    if not ticker.endswith(".NS"):
        return ticker
    sym = ticker[:-3]
    fixed = _normalize_nse_symbol(sym)
    return f"{fixed}.NS" if fixed != sym else ticker


def _is_nse_dummy_symbol(symbol):
    """NSE index CSVs may list internal dummy securities (no Yahoo listing)."""
    upper = symbol.upper()
    return upper.startswith("DUMMY") or upper.endswith("NSETEST")


def _parse_nse_ticker_csv(content_lines):
    """Parses NSE index CSV lines into Yahoo Finance tickers (SYMBOL.NS)."""
    tickers = []
    reader = csv.reader(content_lines)
    header = next(reader)
    symbol_idx = -1
    for idx, col in enumerate(header):
        if "symbol" in col.lower() or "ticker" in col.lower():
            symbol_idx = idx
            break
    if symbol_idx == -1:
        symbol_idx = 2 if len(header) > 2 else 0
    for row in reader:
        if row and len(row) > symbol_idx:
            symbol = row[symbol_idx].strip()
            if symbol and symbol != "Symbol":
                symbol = _normalize_nse_symbol(symbol)
                if _is_nse_dummy_symbol(symbol):
                    continue
                tickers.append(f"{symbol}.NS")
    return tickers


def download_nse_tickers(url):
    """Downloads and parses an official NSE index CSV file to extract ticker symbols."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req) as response:
            content = response.read().decode("utf-8").splitlines()
        return _parse_nse_ticker_csv(content)
    except Exception as e:
        print(f"Warning: Failed to download tickers from {url}: {e}")
    return []

def _parse_yahoo_chart_response(data):
    result = data['chart']['result'][0]
    timestamps = result['timestamp']
    indicators = result['indicators']['quote'][0]
    opens = indicators['open']
    highs = indicators['high']
    lows = indicators['low']
    closes = indicators['close']
    volumes = indicators['volume']

    valid_indices = []
    for i in range(len(timestamps)):
        if (opens[i] is not None and highs[i] is not None and
                lows[i] is not None and closes[i] is not None):
            valid_indices.append(i)

    return {
        "timestamp": [timestamps[i] for i in valid_indices],
        "open": [opens[i] for i in valid_indices],
        "high": [highs[i] for i in valid_indices],
        "low": [lows[i] for i in valid_indices],
        "close": [closes[i] for i in valid_indices],
        "volume": [volumes[i] if volumes[i] is not None else 0 for i in valid_indices],
    }


def _yahoo_http_error_msg(e):
    try:
        body = e.read().decode(errors="replace").strip()
    except Exception:
        body = ""
    snippet = body[:200] if body else ""
    base = f"HTTP {e.code}: {e.reason}"
    return f"{base} ({snippet})" if snippet else base


def fetch_yahoo_data(ticker, *, skip_supabase: bool = False):
    """Fetches ~1 year of daily historical stock data (Supabase cache, then Yahoo)."""
    ticker = _resolve_yahoo_ticker(ticker)
    if not skip_supabase:
        try:
            try:
                from .breakout_ohlcv_store import load_ohlcv_from_supabase, record_yahoo_fetch
            except ImportError:
                from breakout_ohlcv_store import load_ohlcv_from_supabase, record_yahoo_fetch
            sym = ticker.replace(".NS", "").strip().upper()
            cached, cache_err = load_ohlcv_from_supabase(sym, min_bars=50, max_stale_trading_days=3)
            if cached and not cache_err:
                return cached, None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Supabase OHLCV miss %s: %s", ticker, exc)

    try:
        try:
            from .breakout_ohlcv_store import record_yahoo_fetch
        except ImportError:
            from breakout_ohlcv_store import record_yahoo_fetch
        record_yahoo_fetch()
    except Exception:  # noqa: BLE001
        pass

    cache_path = _yahoo_cache_path(ticker)
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            parsed = _parse_yahoo_chart_response(data)
            if len(parsed["close"]) < 50:
                return None, f"only {len(parsed['close'])} bars"
            return parsed, None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, OSError) as e:
            print(f"Yahoo cache read failed {ticker} ({type(e).__name__}), refetching.", flush=True)

    q = urllib.parse.quote(ticker, safe="")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{q}?range=1y&interval=1d"
    transient_backoffs = (2, 4, 8)
    max_attempts = 4
    last_err = None

    for attempt in range(max_attempts):
        _yahoo_pre_request_sleep()
        headers = dict(HEADERS)
        if YAHOO_COOKIE_HEADER:
            headers["Cookie"] = YAHOO_COOKIE_HEADER
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
            parsed = _parse_yahoo_chart_response(data)
            if len(parsed["close"]) < 50:
                last_err = f"only {len(parsed['close'])} bars"
                return None, last_err
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError as e:
                print(f"Yahoo cache write failed {ticker}: {type(e).__name__}", flush=True)
            return parsed, None
        except urllib.error.HTTPError as e:
            last_err = _yahoo_http_error_msg(e)
            if e.code == 429 and attempt < max_attempts - 1:
                wait = YAHOO_429_BACKOFF_SEC[min(attempt, len(YAHOO_429_BACKOFF_SEC) - 1)]
                print(
                    f"Yahoo 429 {ticker}: retry {attempt + 1}/{max_attempts} in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return None, last_err
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            last_err = type(e).__name__
            if attempt < max_attempts - 1:
                wait = transient_backoffs[min(attempt, len(transient_backoffs) - 1)]
                print(
                    f"Yahoo transient {ticker} ({last_err}): retry {attempt + 1}/{max_attempts} in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return None, last_err
        except Exception as e:
            return None, type(e).__name__
    return None, last_err or "fetch_failed"

def calculate_sma(prices, window):
    sma = []
    for i in range(len(prices)):
        if i < window - 1:
            sma.append(0.0)
        else:
            sma.append(sum(prices[i - window + 1:i + 1]) / window)
    return sma

def calculate_rsi(prices, period=14):
    rsi = []
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    if len(deltas) < period:
        return [50.0] * len(prices)
    
    seed_deltas = deltas[:period]
    gains = [d for d in seed_deltas if d > 0]
    losses = [-d for d in seed_deltas if d < 0]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    for i in range(period):
        rsi.append(50.0)
        
    rs = avg_gain / (avg_loss if avg_loss > 0 else 1e-9)
    rsi.append(100.0 - (100.0 / (1.0 + rs)))
    
    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        rs = avg_gain / (avg_loss if avg_loss > 0 else 1e-9)
        rsi.append(100.0 - (100.0 / (1.0 + rs)))
        
    return rsi

def calculate_adx(high, low, close, period=14):
    n = len(close)
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        up_move = high[i] - high[i-1]
        down_move = low[i-1] - low[i]
        
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0.0
            
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0.0
            
    tr_smooth = [0.0] * n
    plus_di_smooth = [0.0] * n
    minus_di_smooth = [0.0] * n
    
    tr_smooth[period] = sum(tr[1:period+1])
    plus_di_smooth[period] = sum(plus_dm[1:period+1])
    minus_di_smooth[period] = sum(minus_dm[1:period+1])
    
    for i in range(period+1, n):
        tr_smooth[i] = tr_smooth[i-1] - (tr_smooth[i-1] / period) + tr[i]
        plus_di_smooth[i] = plus_di_smooth[i-1] - (plus_di_smooth[i-1] / period) + plus_dm[i]
        minus_di_smooth[i] = minus_di_smooth[i-1] - (minus_di_smooth[i-1] / period) + minus_dm[i]
        
    plus_di = [0.0] * n
    minus_di = [0.0] * n
    dx = [0.0] * n
    
    for i in range(period, n):
        den = tr_smooth[i] if tr_smooth[i] else 1e-9
        plus_di[i] = 100.0 * (plus_di_smooth[i] / den)
        minus_di[i] = 100.0 * (minus_di_smooth[i] / den)
        
        num_dx = abs(plus_di[i] - minus_di[i])
        den_dx = plus_di[i] + minus_di[i]
        dx[i] = 100.0 * (num_dx / den_dx if den_dx > 0 else 1e-9)
        
    adx = [0.0] * n
    adx[2*period-1] = sum(dx[period:2*period]) / period
    
    for i in range(2*period, n):
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
        
    return adx, plus_di, minus_di

def get_volume_profile(prices, volumes, bins=10):
    min_p, max_p = min(prices), max(prices)
    if min_p == max_p:
        return min_p
    width = (max_p - min_p) / bins
    edges = [min_p + i * width for i in range(bins+1)]
    vols = [0.0] * bins
    
    for p, v in zip(prices, volumes):
        idx = -1
        for j in range(bins):
            if edges[j] <= p <= edges[j+1]:
                idx = j
                break
        if idx != -1:
            vols[idx] += v
            
    poc_idx = vols.index(max(vols))
    return round((edges[poc_idx] + edges[poc_idx+1]) / 2, 2)


def _backtest_yahoo_cache_dir() -> str:
    cache_path = _repo_root() / "data" / "cache" / "breakout_yahoo" / "backtest"
    os.makedirs(cache_path, exist_ok=True)
    return str(cache_path)


def _yahoo_backtest_cache_path(ticker: str, range_str: str = "6m") -> str:
    safe = ticker.replace("/", "_").replace("\\", "_")
    return os.path.join(_backtest_yahoo_cache_dir(), f"{safe}_{range_str}.json")


def fetch_yahoo_history(
    ticker: str,
    *,
    range_str: str = "6m",
    min_bars: int = 50,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch daily OHLCV from Yahoo chart API with a backtest-specific on-disk cache."""
    cache_path = _yahoo_backtest_cache_path(ticker, range_str)
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            parsed = _parse_yahoo_chart_response(data)
            if len(parsed["close"]) >= min_bars:
                return parsed, None
            print(
                f"Yahoo backtest cache stale {ticker} ({len(parsed['close'])} bars), refetching.",
                flush=True,
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, OSError) as e:
            print(f"Yahoo backtest cache read failed {ticker} ({type(e).__name__}), refetching.", flush=True)

    q = urllib.parse.quote(ticker, safe="")
    if range_str == "6m":
        period2 = int(time.time())
        period1 = period2 - 180 * 24 * 3600
        url = (
            f"https://query2.finance.yahoo.com/v8/finance/chart/{q}"
            f"?period1={period1}&period2={period2}&interval=1d"
        )
    else:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{q}?range={range_str}&interval=1d"
    transient_backoffs = (2, 4, 8)
    max_attempts = 4
    last_err = None

    for attempt in range(max_attempts):
        _yahoo_pre_request_sleep()
        headers = dict(HEADERS)
        if YAHOO_COOKIE_HEADER:
            headers["Cookie"] = YAHOO_COOKIE_HEADER
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
            parsed = _parse_yahoo_chart_response(data)
            if len(parsed["close"]) < min_bars:
                last_err = f"only {len(parsed['close'])} bars"
                return None, last_err
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError as e:
                print(f"Yahoo backtest cache write failed {ticker}: {type(e).__name__}", flush=True)
            return parsed, None
        except urllib.error.HTTPError as e:
            last_err = _yahoo_http_error_msg(e)
            if e.code == 429 and attempt < max_attempts - 1:
                wait = YAHOO_429_BACKOFF_SEC[min(attempt, len(YAHOO_429_BACKOFF_SEC) - 1)]
                print(
                    f"Yahoo 429 {ticker}: retry {attempt + 1}/{max_attempts} in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return None, last_err
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            last_err = type(e).__name__
            if attempt < max_attempts - 1:
                wait = transient_backoffs[min(attempt, len(transient_backoffs) - 1)]
                print(
                    f"Yahoo transient {ticker} ({last_err}): retry {attempt + 1}/{max_attempts} in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return None, last_err
        except Exception as e:
            return None, type(e).__name__
    return None, last_err or "fetch_failed"


def bar_dates_from_df(df: dict[str, Any]) -> list[str]:
    """Map Yahoo bar timestamps to ISO trade dates (IST)."""
    out: list[str] = []
    for ts in df.get("timestamp") or []:
        try:
            out.append(datetime.datetime.fromtimestamp(int(ts), tz=IST).date().isoformat())
        except (TypeError, ValueError, OSError):
            out.append("")
    return out


def _prior_volume_spike(
    volume: list[float],
    vol_20_avg: list[float],
    vol_thresh: float,
    as_of_idx: int,
    lookback: int = 2,
) -> bool:
    """True when any of the prior `lookback` sessions cleared the tier vol threshold."""
    start = max(20, as_of_idx - lookback)
    for i in range(start, as_of_idx):
        avg = vol_20_avg[i] if vol_20_avg[i] > 0 else 1.0
        if volume[i] / avg >= vol_thresh:
            return True
    return False


def _prices_equal(a: float, b: float, *, rel_tol: float = 1e-6) -> bool:
    """Float-safe equality for OHLC comparisons (e.g. upper-circuit lock)."""
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= rel_tol * scale


def is_upper_circuit_locked(
    close: float,
    high: float,
    pct_change: float,
    *,
    pct_min: float = UPPER_CIRCUIT_PCT_MIN,
) -> bool:
    """True when close pins the day high with a circuit-scale daily gain (~5/10/20% bands)."""
    return _prices_equal(close, high) and pct_change >= pct_min


def _benchmark_as_of_idx(
    df: dict[str, Any],
    signal_date: datetime.date | str | None,
) -> int:
    """Bar index for point-in-time regime (last bar on or before signal_date)."""
    n = len(df["close"])
    if signal_date is None:
        return n - 1
    target = str(signal_date)[:10]
    dates = bar_dates_from_df(df)
    as_of_idx = n - 1
    for i, d in enumerate(dates):
        if d == target:
            return i
        if d and d > target:
            return max(0, i - 1)
    return as_of_idx


def evaluate_market_regime(
    benchmark_df: dict[str, Any] | None = None,
    *,
    benchmark_ticker: str = MARKET_REGIME_BENCHMARK,
    signal_date: datetime.date | str | None = None,
) -> dict[str, Any]:
    """Benchmark trend gate: RISK_OFF when index close is below its 20-day SMA.

    When ``signal_date`` is set, uses benchmark bars on or before that date only
    (point-in-time for backtest replay).
    """
    df = benchmark_df
    fetch_err: str | None = None
    if df is None:
        df, fetch_err = fetch_yahoo_data(benchmark_ticker)
    n = len(df["close"]) if df and df.get("close") else 0
    if not df or n < MARKET_REGIME_SMA_WINDOW:
        return {
            "market_regime": "UNKNOWN",
            "benchmark_ticker": benchmark_ticker,
            "benchmark_close": None,
            "benchmark_sma20": None,
            "signal_date": str(signal_date)[:10] if signal_date else None,
            "fetch_error": fetch_err or "insufficient_benchmark_bars",
        }
    as_of_idx = _benchmark_as_of_idx(df, signal_date)
    if as_of_idx + 1 < MARKET_REGIME_SMA_WINDOW:
        return {
            "market_regime": "UNKNOWN",
            "benchmark_ticker": benchmark_ticker,
            "benchmark_close": None,
            "benchmark_sma20": None,
            "signal_date": str(signal_date)[:10] if signal_date else None,
            "fetch_error": "insufficient_benchmark_bars_as_of_date",
        }
    closes = df["close"][: as_of_idx + 1]
    sma20 = calculate_sma(closes, MARKET_REGIME_SMA_WINDOW)
    last_close = float(closes[-1])
    last_sma = float(sma20[-1])
    regime = "RISK_OFF" if last_close < last_sma else "RISK_ON"
    benchmark_5d_return = return_5d_pct(closes, as_of_idx)
    return {
        "market_regime": regime,
        "benchmark_ticker": benchmark_ticker,
        "benchmark_close": round(last_close, 4),
        "benchmark_sma20": round(last_sma, 4),
        "benchmark_5d_return": benchmark_5d_return,
        "signal_date": str(signal_date)[:10] if signal_date else None,
        "benchmark_as_of_idx": as_of_idx,
        "benchmark_df": df,
        "fetch_error": fetch_err,
    }


def _volume_filter_passes(
    *,
    vol_mult: float,
    vol_cum_mult: float,
    vol_thresh: float,
    tier_name: str,
    prior_spike: bool,
) -> tuple[bool, str | None]:
    """Standard spike, 3-day cumulative continuation, or micro-cap post-spike path."""
    if vol_mult >= vol_thresh:
        return True, None
    if vol_cum_mult >= vol_thresh:
        return True, "vol_continuation_cum3d"
    if tier_name == "MICRO_CAP_250" and prior_spike and vol_mult >= MICRO_CAP_VOL_CONTINUATION:
        return True, "vol_continuation_prior_spike"
    return False, None


def pre_signal_validation(
    df: dict[str, Any],
    as_of_idx: int,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Validate pre-signal history on bars strictly before signal day T (as_of_idx).

    Rules (evaluated on T-15..T-1, or max available bars before T):
    - Skip if cumulative return T-10 → T-1 exceeds +30%
    - Skip if more than 2 days in the lookback window had volume > 2× 20d avg
    """
    closes = df["close"]
    volumes = df["volume"]
    t = as_of_idx
    lookback = min(PRE_SIGNAL_FULL_LOOKBACK, t)
    w_start = t - lookback
    w_end = t - 1

    vol_20_avg = calculate_sma(volumes[: t + 1], 20)

    cum_return_t10_t1: float | None = None
    if t >= PRE_SIGNAL_CUM_RETURN_LOOKBACK:
        close_t10 = closes[t - PRE_SIGNAL_CUM_RETURN_LOOKBACK]
        close_t1 = closes[t - 1]
        if close_t10 > 0:
            cum_return_t10_t1 = (close_t1 / close_t10 - 1.0) * 100.0

    vol_spike_days = 0
    if w_end >= w_start >= 0:
        for i in range(w_start, w_end + 1):
            vma = vol_20_avg[i] if i < len(vol_20_avg) else 0.0
            if vma > 0 and volumes[i] > PRE_SIGNAL_VOL_SPIKE_MULT * vma:
                vol_spike_days += 1

    metrics: dict[str, Any] = {
        "cum_return_t10_t1": round(cum_return_t10_t1, 4) if cum_return_t10_t1 is not None else None,
        "vol_spike_days_t15_t1": vol_spike_days,
        "pre_window_bars": lookback,
        "pre_window_start_offset": -lookback,
        "full_window": t >= PRE_SIGNAL_FULL_LOOKBACK,
        "cum_return_window_bars": PRE_SIGNAL_CUM_RETURN_LOOKBACK if t >= PRE_SIGNAL_CUM_RETURN_LOOKBACK else None,
    }

    if cum_return_t10_t1 is not None and cum_return_t10_t1 > PRE_SIGNAL_CUM_RETURN_MAX:
        return False, "pre_filter_cum_return", metrics

    if vol_spike_days > PRE_SIGNAL_VOL_SPIKE_DAYS_MAX:
        return False, "pre_filter_vol_spike", metrics

    return True, None, metrics


def _pre_signal_cooldown_metrics(
    df: dict[str, Any],
    as_of_idx: int,
) -> dict[str, Any]:
    """Consolidation range (T-10..T-1) and distance from 20d high at T-1."""
    t = as_of_idx
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    w10_start = t - PRE_SIGNAL_CUM_RETURN_LOOKBACK
    w_end = t - 1
    close_t1 = closes[t - 1]

    cons_high = max(highs[w10_start : w_end + 1])
    cons_low = min(lows[w10_start : w_end + 1])
    consolidation_range_pct = (
        (cons_high - cons_low) / close_t1 * 100.0 if close_t1 > 0 else 0.0
    )

    high_20_start = max(0, t - 20)
    high_20 = max(highs[high_20_start:t])
    dist_from_20d_high_pct = (
        (close_t1 / high_20 - 1.0) * 100.0 if high_20 > 0 else 0.0
    )

    return {
        "consolidation_range_pct": round(consolidation_range_pct, 4),
        "dist_from_20d_high_pct": round(dist_from_20d_high_pct, 4),
    }


def _adx_trajectory_gate(
    adx_arr: list[float],
    as_of_idx: int,
    pass_paths: list[str],
    *,
    vol_mult: float | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Require rising ADX on standard or adx_soft paths.

    Standard path (empty pass_paths): falling ADX allowed when vol_mult >= 7.0.
    """
    is_standard = not pass_paths
    needs_gate = is_standard or "adx_soft" in pass_paths
    t = as_of_idx
    metrics: dict[str, Any] = {
        "adx_trajectory_required": needs_gate,
        "standard_path": is_standard,
        "adx_t1": None,
        "adx_t10": None,
        "adx_t5": None,
        "standard_vol_exception": False,
    }
    if not needs_gate:
        return True, None, metrics

    fail_reason = (
        "pre_filter_standard_adx_trajectory" if is_standard else "pre_filter_adx_trajectory"
    )
    if t < PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK:
        return False, fail_reason, metrics

    adx_t1 = adx_arr[t - 1]
    adx_t10 = adx_arr[t - PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK]
    adx_t5 = (
        adx_arr[t - PRE_SIGNAL_ADX_SOFT_SHORT_LOOKBACK]
        if t >= PRE_SIGNAL_ADX_SOFT_SHORT_LOOKBACK
        else None
    )
    metrics.update({
        "adx_t1": round(adx_t1, 4),
        "adx_t10": round(adx_t10, 4),
        "adx_t5": round(adx_t5, 4) if adx_t5 is not None else None,
    })

    if adx_t1 <= adx_t10:
        if (
            is_standard
            and vol_mult is not None
            and vol_mult >= STANDARD_ADX_TRAJECTORY_VOL_EXCEPTION
        ):
            metrics["standard_vol_exception"] = True
            return True, None, metrics
        return False, fail_reason, metrics

    if "adx_soft" in pass_paths and adx_t5 is not None and adx_t1 <= adx_t5:
        return False, "pre_filter_adx_trajectory", metrics

    return True, None, metrics


def _signal_cooldown_gate(
    df: dict[str, Any],
    as_of_idx: int,
    last_pass_idx: int | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Block repeat PASS within cooldown unless consolidation + proximity exemptions."""
    metrics: dict[str, Any] = {"last_pass_idx": last_pass_idx, "sessions_since_prior_pass": None}
    if last_pass_idx is None:
        return True, None, metrics

    sessions_since = as_of_idx - last_pass_idx
    metrics["sessions_since_prior_pass"] = sessions_since
    if sessions_since <= 0 or sessions_since > PRE_SIGNAL_COOLDOWN_SESSIONS:
        return True, None, metrics

    cooldown_metrics = _pre_signal_cooldown_metrics(df, as_of_idx)
    metrics.update(cooldown_metrics)
    exempt = (
        cooldown_metrics["consolidation_range_pct"] <= PRE_SIGNAL_COOLDOWN_CONSOLIDATION_MAX
        and cooldown_metrics["dist_from_20d_high_pct"] >= PRE_SIGNAL_COOLDOWN_DIST_20D_HIGH_MIN
    )
    metrics["cooldown_exempt"] = exempt
    if exempt:
        return True, None, metrics

    return False, "pre_filter_signal_cooldown", metrics


def _adx_soft_chase_gate(
    cum_return_t10_t1: float | None,
    pass_paths: list[str],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Block adx_soft when pre-trend run-up exceeds path-specific cap."""
    metrics: dict[str, Any] = {
        "adx_soft_chase_required": "adx_soft" in pass_paths,
        "cum_return_t10_t1": round(cum_return_t10_t1, 4) if cum_return_t10_t1 is not None else None,
        "adx_soft_cum_return_max": ADX_SOFT_CUM_RETURN_MAX,
    }
    if "adx_soft" not in pass_paths:
        return True, None, metrics
    if cum_return_t10_t1 is not None and cum_return_t10_t1 > ADX_SOFT_CUM_RETURN_MAX:
        return False, "pre_filter_adx_soft_chase", metrics
    return True, None, metrics


def _power_gap_confirmation_gate(
    adx_arr: list[float],
    as_of_idx: int,
    cum_return_t10_t1: float | None,
    pass_paths: list[str],
    *,
    vol_mult: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """power_gap requires rising ADX or modest pre-trend; otherwise downgrade to WATCH.

    High-volume gaps (vol_mult >= 5.5) may PASS without ADX/cum_ret confirmation.
    """
    needs_gate = "power_gap" in pass_paths
    metrics: dict[str, Any] = {
        "power_gap_confirmation_required": needs_gate,
        "adx_t1": None,
        "adx_t10": None,
        "cum_return_t10_t1": round(cum_return_t10_t1, 4) if cum_return_t10_t1 is not None else None,
        "power_gap_cum_return_max": POWER_GAP_CUM_RETURN_MAX,
        "power_gap_vol_recovery_threshold": POWER_GAP_VOL_RECOVERY_THRESHOLD,
        "vol_mult": round(vol_mult, 4) if vol_mult is not None else None,
        "adx_rising": None,
        "vol_recovery": None,
        "confirmed": None,
    }
    if not needs_gate:
        metrics["confirmed"] = True
        return True, metrics

    t = as_of_idx
    adx_rising = False
    if t >= PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK:
        adx_t1 = adx_arr[t - 1]
        adx_t10 = adx_arr[t - PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK]
        adx_rising = adx_t1 > adx_t10
        metrics.update({
            "adx_t1": round(adx_t1, 4),
            "adx_t10": round(adx_t10, 4),
            "adx_rising": adx_rising,
        })

    cum_ok = cum_return_t10_t1 is not None and cum_return_t10_t1 <= POWER_GAP_CUM_RETURN_MAX
    vol_recovery = vol_mult is not None and vol_mult >= POWER_GAP_VOL_RECOVERY_THRESHOLD
    metrics["vol_recovery"] = vol_recovery
    confirmed = adx_rising or cum_ok or vol_recovery
    metrics["confirmed"] = confirmed
    return confirmed, metrics


def _format_risk_flags(
    *,
    power_gap: bool,
    pass_paths: list[str],
    signal_tier: str | None = None,
    upper_circuit: bool = False,
    extra_flags: list[str] | None = None,
) -> str:
    if signal_tier == SIGNAL_TIER_WATCH:
        parts: list[str] = ["WATCHLIST"]
        if upper_circuit:
            parts.append("UPPER_CIRCUIT")
        for flag in extra_flags or []:
            if flag and flag not in parts:
                parts.append(flag)
        if power_gap:
            parts.append("circuit-risk")
        suffix = (
            "WATCH only — enforce 1% sizing cap; no new entry unless ADX resumes rising. "
            "Audit GSM/ASM status."
        )
        if pass_paths:
            suffix += f" Pass paths: {', '.join(pass_paths)}."
        return ": ".join(parts) + f": {suffix}"

    parts: list[str] = []
    if upper_circuit:
        parts.append("UPPER_CIRCUIT")
    for flag in extra_flags or []:
        if flag and flag not in parts:
            parts.append(flag)
    if power_gap:
        parts.append("circuit-risk")
    parts.append("HIGH CIRCUIT RISK")
    suffix = "Enforce tight capital sizing (1-2%). Audit GSM/ASM status."
    if pass_paths:
        suffix += f" Pass paths: {', '.join(pass_paths)}."
    return ": ".join(parts) + (f": {suffix}" if parts else suffix)


_V7_FAIL_REASONS = frozenset({
    "pre_filter_cum_return",
    "pre_filter_vol_spike",
    "pre_filter_standard_adx_trajectory",
    "pre_filter_adx_trajectory",
    "pre_filter_signal_cooldown",
    "pre_filter_adx_soft_chase",
    "pre_filter_liquidity",
    "missing_liquidity_data",
    "pre_filter_micro_participation",
    "v7_low_volume_persistence",
    "v7_breakout_stage_3",
    "circuit_locked",
    "excessive_alternate_paths",
    "market_regime_risk_off",
    "upper_wick_rejection",
    "imminent_earnings",
    "distribution_base",
})


def _classic_filter_failures(eval_result: dict[str, Any], tier_name: str) -> list[str]:
    """Return classic breakout filters that fail independently on this bar."""
    filt = FILTERS[tier_name]
    failures: list[str] = []
    price = eval_result.get("latest_price", 0.0)
    if price < filt["min_price"]:
        failures.append("min_price")

    pct = eval_result.get("pct_change", 0.0)
    if pct < PCT_CHANGE_MIN or pct > PCT_CHANGE_MAX_POWER_GAP:
        failures.append("pct_change")

    sma50 = eval_result.get("sma50_last", 0.0)
    sma20 = eval_result.get("sma20_last", 0.0)
    vol_mult = eval_result.get("vol_mult", 0.0)
    if price < sma50:
        sma20_reclaim = price >= sma20 and vol_mult >= SMA20_RECLAIM_VOL_THRESHOLD
        if not sma20_reclaim:
            failures.append("SMA50")

    vol_cum = eval_result.get("vol_cum_mult", 0.0)
    prior_spike = eval_result.get("prior_volume_spike", False)
    vol_ok, _ = _volume_filter_passes(
        vol_mult=vol_mult,
        vol_cum_mult=vol_cum,
        vol_thresh=filt["vol_mult"],
        tier_name=tier_name,
        prior_spike=prior_spike,
    )
    if not vol_ok:
        failures.append("vol")

    rsi = eval_result.get("rsi_val", 0.0)
    if rsi < RSI_MIN:
        failures.append("RSI")

    adx = eval_result.get("adx_val", 0.0)
    adx_ok = adx >= ADX_HARD_FLOOR
    if (
        not adx_ok
        and ADX_SOFT_FLOOR <= adx < ADX_HARD_FLOOR
        and vol_mult >= filt["vol_mult"]
        and price > sma50
        and pct > 0
    ):
        adx_ok = True
    if not adx_ok:
        failures.append("ADX")

    close_pos = eval_result.get("close_position")
    if close_pos is not None and float(close_pos) < CLOSE_POSITION_MIN:
        failures.append("upper_wick_rejection")

    if eval_result.get("target_gain", 0.0) < 8.0:
        failures.append("target_gain")

    base_acc = eval_result.get("base_accumulation") or {}
    if base_acc.get("passed") is False:
        failures.append("distribution_base")
    return failures


def collect_filter_failures(eval_result: dict[str, Any], tier_name: str) -> list[str]:
    """Every production filter / v7 gate that fails on this bar (for missed-breakout analysis)."""
    failures = _classic_filter_failures(eval_result, tier_name)
    seen = set(failures)

    def _add(tag: str) -> None:
        if tag and tag not in seen:
            failures.append(tag)
            seen.add(tag)

    evidence = eval_result.get("evidence") or {}
    if not evidence.get("liquidity_gate_pass", True):
        _add(evidence.get("liquidity_gate_fail") or "pre_filter_liquidity")

    if evidence.get("micro_participation_pass") is False:
        _add("pre_filter_micro_participation")

    fail_reason = eval_result.get("fail_reason")
    if fail_reason and (
        fail_reason in _V7_FAIL_REASONS or str(fail_reason).startswith("pre_filter_")
    ):
        _add(str(fail_reason))

    watch = eval_result.get("v7_watch_reason")
    if watch and eval_result.get("signal_tier") == SIGNAL_TIER_WATCH:
        _add(str(watch))

    return failures


def evaluate_bars_as_of(
    df: dict[str, Any],
    as_of_idx: int,
    tier_name: str,
    *,
    last_pass_idx: int | None = None,
    bhav_turnover_lacs: float | None = None,
    delivery_pct: float | None = None,
    avg_delivery_pct: float | None = None,
    free_float_pct: float | None = None,
    sector_lead: float | None = None,
    market_regime: str | None = None,
    days_to_next_earnings: int | None = None,
    benchmark_5d_return: float | None = None,
) -> dict[str, Any]:
    """Point-in-time breakout filter evaluation using bars[0..as_of_idx] inclusive.

    Alternate pass paths (tracked in pass_paths / risk_flags):
    - power_gap: pct_change 12–20% with circuit-risk flag
    - vol_continuation_cum3d: 3-session cumulative vol >= tier threshold
    - vol_continuation_prior_spike: micro-cap 2.5x after prior spike session
    - sma20_reclaim: price > SMA20 with vol > 5x despite below SMA50
    - adx_soft: ADX 20–25 with strong vol + above SMA50 + positive day
    """
    filt = FILTERS[tier_name]
    vol_thresh = filt["vol_mult"]
    n = as_of_idx + 1
    if n < 50:
        return {
            "passed": False,
            "fail_reason": "insufficient_data",
            "bar_count": n,
            "as_of_idx": as_of_idx,
        }

    close = df["close"][:n]
    high = df["high"][:n]
    low = df["low"][:n]
    volume = df["volume"][:n]

    latest_price = close[-1]
    prev_price = close[-2]
    pct_change = ((latest_price - prev_price) / prev_price) * 100

    sma_20 = calculate_sma(close, 20)
    sma_50 = calculate_sma(close, 50)
    vol_20_avg = calculate_sma(volume, 20)
    vol_mult = volume[-1] / (vol_20_avg[-1] if vol_20_avg[-1] > 0 else 1.0)
    cum_days = min(VOL_CUM_DAYS, n)
    vol_cum_mult = (
        sum(volume[-cum_days:]) / (cum_days * vol_20_avg[-1])
        if vol_20_avg[-1] > 0
        else 0.0
    )
    prior_spike = _prior_volume_spike(volume, vol_20_avg, vol_thresh, as_of_idx)
    rsi = calculate_rsi(close, 14)
    adx_arr, _, _ = calculate_adx(high, low, close, period=14)
    rsi_val = rsi[-1]
    adx_val = adx_arr[-1]
    sma20_last = sma_20[-1]
    t = as_of_idx
    sma50_last = float(sma_50[t - 1]) if t >= 1 else float(sma_50[-1])
    vol_20_avg_last = vol_20_avg[-1]
    latest_volume = volume[-1]

    sma_200 = calculate_sma(close, 200) if len(close) > 200 else [0.0] * len(close)
    sma_200_last = sma_200[-1] if sma_200 else None
    poc_start = max(0, t - 30)
    poc_window = close[poc_start:t] if t > poc_start else close[:t]
    vol_window = volume[poc_start:t] if t > poc_start else volume[:t]
    if not poc_window:
        poc_window = close[:1]
        vol_window = volume[:1]
    poc = get_volume_profile(poc_window, vol_window)

    if t > 0:
        w_start = max(0, t - 20)
        recent_20d_swing_low = min(float(l) for l in low[w_start:t])
    else:
        recent_20d_swing_low = float(low[0])
    atr_arr = _atr_simple(high, low, close, period=14)
    atr_14 = float(atr_arr[-1]) if atr_arr[-1] > 0 else 0.0
    atr_stop = latest_price - (2.5 * atr_14) if atr_14 > 0 else recent_20d_swing_low
    sl_price = max(recent_20d_swing_low, atr_stop)
    risk = latest_price - sl_price
    target_price = latest_price + (2.0 * risk)
    target_gain = ((target_price - latest_price) / latest_price) * 100

    daily_range = float(high[-1]) - float(low[-1])
    if daily_range > 0:
        close_position = round((latest_price - float(low[-1])) / daily_range, 4)
    else:
        close_position = 1.0

    stock_5d_return = return_5d_pct(close, t)
    rel_return_5d = relative_strength_vs_benchmark(stock_5d_return, benchmark_5d_return)

    pass_paths: list[str] = []
    power_gap = False
    extra_risk_flags: list[str] = []

    metrics = {
        "latest_price": round(latest_price, 2),
        "prev_price": round(prev_price, 2),
        "pct_change": round(pct_change, 4),
        "vol_mult": round(vol_mult, 4),
        "vol_cum_mult": round(vol_cum_mult, 4),
        "prior_volume_spike": prior_spike,
        "rsi_val": round(rsi_val, 2),
        "adx_val": round(adx_val, 2),
        "sma20_last": round(sma20_last, 2),
        "sma50_last": round(sma50_last, 2),
        "sma_200_last": round(sma_200_last, 2) if sma_200_last is not None else None,
        "poc": poc,
        "close_position": round(close_position, 4),
        "daily_range": round(daily_range, 4),
        "stock_5d_return": stock_5d_return,
        "benchmark_5d_return": benchmark_5d_return,
        "rel_return_5d_vs_benchmark": rel_return_5d,
        "atr_14": round(atr_14, 4),
        "recent_20d_swing_low": round(recent_20d_swing_low, 2),
        "vol_20_avg_last": round(vol_20_avg_last, 2),
        "latest_volume": latest_volume,
        "sl_price": round(sl_price, 2),
        "target_price": round(target_price, 2),
        "target_gain": round(target_gain, 4),
        "entry_low": round(latest_price * 0.985, 2),
        "entry_high": round(latest_price * 1.01, 2),
        "bar_count": n,
        "as_of_idx": as_of_idx,
        "min_price_threshold": filt["min_price"],
        "vol_mult_threshold": vol_thresh,
        "pass_paths": pass_paths,
        "risk_flags": "",
    }

    fail_reason = None
    if latest_price < filt["min_price"]:
        fail_reason = "min_price"
    elif pct_change < PCT_CHANGE_MIN or pct_change > PCT_CHANGE_MAX_POWER_GAP:
        fail_reason = "pct_change"
    elif PCT_CHANGE_MAX_NORMAL < pct_change <= PCT_CHANGE_MAX_POWER_GAP:
        power_gap = True
        pass_paths.append("power_gap")

    if fail_reason is None and latest_price < sma50_last:
        sma20_reclaim = (
            latest_price >= sma20_last
            and vol_mult >= SMA20_RECLAIM_VOL_THRESHOLD
        )
        if sma20_reclaim:
            pass_paths.append("sma20_reclaim")
        else:
            fail_reason = "SMA50"

    if fail_reason is None:
        vol_ok, vol_path = _volume_filter_passes(
            vol_mult=vol_mult,
            vol_cum_mult=vol_cum_mult,
            vol_thresh=vol_thresh,
            tier_name=tier_name,
            prior_spike=prior_spike,
        )
        if not vol_ok:
            fail_reason = "vol"
        elif vol_path:
            pass_paths.append(vol_path)

    if fail_reason is None and close_position < CLOSE_POSITION_MIN:
        fail_reason = "upper_wick_rejection"

    if fail_reason is None:
        if rsi_val < RSI_MIN:
            fail_reason = "RSI"

    if fail_reason is None:
        adx_ok = adx_val >= ADX_HARD_FLOOR
        if (
            not adx_ok
            and ADX_SOFT_FLOOR <= adx_val < ADX_HARD_FLOOR
            and vol_mult >= vol_thresh + ADX_SOFT_VOL_BONUS
            and latest_price > sma50_last
            and pct_change > 0
        ):
            adx_ok = True
            pass_paths.append("adx_soft")
        if not adx_ok:
            fail_reason = "ADX"

    if fail_reason is None and target_gain < 8.0:
        fail_reason = "target_gain"

    pre_filter_fail: str | None = None
    if fail_reason is None:
        pre_ok, pre_fail, pre_metrics = pre_signal_validation(df, as_of_idx)
        metrics["pre_validation"] = pre_metrics
        if not pre_ok:
            pre_filter_fail = pre_fail
            fail_reason = pre_fail

    if fail_reason is None:
        accum_ok, accum_metrics = base_accumulation_pass(
            df["open"], df["close"], df["volume"], as_of_idx,
        )
        metrics["base_accumulation"] = accum_metrics
        if not accum_ok:
            fail_reason = "distribution_base"

    if fail_reason is None:
        traj_ok, traj_fail, traj_metrics = _adx_trajectory_gate(
            adx_arr, as_of_idx, pass_paths, vol_mult=vol_mult,
        )
        metrics["adx_trajectory"] = traj_metrics
        if not traj_ok:
            pre_filter_fail = traj_fail
            fail_reason = traj_fail

    if fail_reason is None:
        cd_ok, cd_fail, cd_metrics = _signal_cooldown_gate(df, as_of_idx, last_pass_idx)
        metrics["signal_cooldown"] = cd_metrics
        if not cd_ok:
            pre_filter_fail = cd_fail
            fail_reason = cd_fail

    cum_return_t10_t1 = (metrics.get("pre_validation") or {}).get("cum_return_t10_t1")

    if fail_reason is None:
        chase_ok, chase_fail, chase_metrics = _adx_soft_chase_gate(cum_return_t10_t1, pass_paths)
        metrics["adx_soft_chase"] = chase_metrics
        if not chase_ok:
            pre_filter_fail = chase_fail
            fail_reason = chase_fail

    signal_tier: str | None = SIGNAL_TIER_PASS if fail_reason is None else None
    if fail_reason is None and "power_gap" in pass_paths:
        pg_ok, pg_metrics = _power_gap_confirmation_gate(
            adx_arr, as_of_idx, cum_return_t10_t1, pass_paths, vol_mult=vol_mult,
        )
        metrics["power_gap_confirmation"] = pg_metrics
        if not pg_ok:
            signal_tier = SIGNAL_TIER_WATCH

    if fail_reason is None and pass_paths == ["adx_soft"]:
        signal_tier = SIGNAL_TIER_WATCH
        metrics["adx_soft_solo_watch"] = True

    upper_circuit = is_upper_circuit_locked(latest_price, float(high[-1]), pct_change)
    metrics["upper_circuit_locked"] = upper_circuit

    alternate_bypasses = [p for p in pass_paths if p != "power_gap"]
    metrics["alternate_bypass_count"] = len(alternate_bypasses)
    if fail_reason is None and len(alternate_bypasses) > 1:
        signal_tier = SIGNAL_TIER_WATCH
        metrics["excessive_alternate_paths"] = True

    evidence = compute_evidence_metrics(
        df,
        as_of_idx,
        tier_name,
        vol_20_avg,
        bhav_turnover_lacs=bhav_turnover_lacs,
        delivery_pct=delivery_pct,
        avg_delivery_pct=avg_delivery_pct,
        free_float_pct=free_float_pct,
    )
    metrics["evidence"] = evidence
    metrics["liquidity_quality"] = evidence.get("liquidity_quality")
    metrics["persistence_score"] = evidence.get("persistence_score")
    metrics["breakout_stage"] = evidence.get("breakout_stage")
    metrics["base_score"] = evidence.get("base_score")
    metrics["median_turnover_inr"] = evidence.get("median_turnover_inr")
    metrics["delivery_pct"] = evidence.get("delivery_pct")
    metrics["avg_delivery_pct"] = evidence.get("avg_delivery_pct")
    metrics["free_float_pct"] = evidence.get("free_float_pct")
    metrics["vpr"] = evidence.get("vpr")
    metrics["cmf"] = evidence.get("cmf")
    metrics["sector_lead"] = sector_lead
    part_risk = evidence.get("participation_risk_flag")
    if part_risk:
        extra_risk_flags.append(str(part_risk))

    if fail_reason is None and not evidence.get("liquidity_gate_pass", True):
        pre_filter_fail = evidence.get("liquidity_gate_fail") or "pre_filter_liquidity"
        fail_reason = pre_filter_fail
        signal_tier = None

    if fail_reason is None:
        part = evidence.get("micro_participation_pass")
        if part is False:
            pre_filter_fail = "pre_filter_micro_participation"
            fail_reason = pre_filter_fail
            signal_tier = None

    v7_watch_reason: str | None = None
    if fail_reason is None and signal_tier == SIGNAL_TIER_PASS:
        persist_min = persistence_pass_min(tier_name)
        if (evidence.get("persistence_score") or 0) < persist_min:
            signal_tier = SIGNAL_TIER_WATCH
            v7_watch_reason = "v7_low_volume_persistence"
        elif evidence.get("breakout_stage") == 3:
            signal_tier = SIGNAL_TIER_WATCH
            v7_watch_reason = "v7_breakout_stage_3"

    if fail_reason is None and upper_circuit and signal_tier == SIGNAL_TIER_PASS:
        signal_tier = SIGNAL_TIER_WATCH
        v7_watch_reason = "circuit_locked"

    if fail_reason is None and metrics.get("excessive_alternate_paths"):
        if signal_tier == SIGNAL_TIER_PASS:
            signal_tier = SIGNAL_TIER_WATCH
        if v7_watch_reason is None:
            v7_watch_reason = "excessive_alternate_paths"

    if (
        fail_reason is None
        and market_regime == "RISK_OFF"
        and signal_tier == SIGNAL_TIER_PASS
    ):
        signal_tier = SIGNAL_TIER_WATCH
        v7_watch_reason = "market_regime_risk_off"
        metrics["market_regime"] = market_regime

    if fail_reason is None and days_to_next_earnings is not None:
        metrics["days_to_next_earnings"] = days_to_next_earnings
        if days_to_next_earnings <= IMMINENT_EARNINGS_DAYS:
            extra_risk_flags.append("IMMINENT_EARNINGS")
            if signal_tier == SIGNAL_TIER_PASS:
                signal_tier = SIGNAL_TIER_WATCH
                v7_watch_reason = "imminent_earnings"

    if v7_watch_reason:
        metrics["v7_watch_reason"] = v7_watch_reason

    metrics["composite_rank"] = composite_rank_score(metrics, sector_lead=sector_lead)

    metrics["pass_paths"] = pass_paths
    metrics["signal_tier"] = signal_tier
    metrics["risk_flags"] = _format_risk_flags(
        power_gap=power_gap,
        pass_paths=pass_paths,
        signal_tier=signal_tier,
        upper_circuit=upper_circuit,
        extra_flags=extra_risk_flags,
    )

    return {
        "passed": fail_reason is None and signal_tier == SIGNAL_TIER_PASS,
        "signal_tier": signal_tier,
        "fail_reason": fail_reason,
        "pre_filter_fail": pre_filter_fail,
        **metrics,
    }


def _emit_stock_diagnostic(emit, ticker, tier_name, latest_price=None, pct_change=None,
                           vol_mult=None, vol_thresh=None, rsi_val=None, adx_val=None,
                           sma50=None, status="FAIL", fail_reason=""):
    sym = ticker.replace(".NS", "")
    if latest_price is None:
        line = f"{sym} | tier={tier_name} | FAIL: {fail_reason or 'no_data'}"
    else:
        chg_s = f"{pct_change:+.2f}%" if pct_change is not None else "n/a"
        vol_s = f"{vol_mult:.2f}x" if vol_mult is not None else "n/a"
        need = f" (need>={vol_thresh})" if vol_thresh is not None else ""
        rsi_s = f"{rsi_val:.1f}" if rsi_val is not None else "n/a"
        adx_s = f"{adx_val:.1f}" if adx_val is not None else "n/a"
        if sma50 is not None and sma50 > 0:
            rel = "above" if latest_price >= sma50 else "below"
            sma_s = f"{sma50:.2f} {rel}"
        else:
            sma_s = "n/a"
        tail = "PASS" if status == "PASS" else (f"FAIL: {fail_reason}" if fail_reason else "FAIL")
        line = (
            f"{sym} | price={latest_price:.2f} chg={chg_s} | "
            f"vol={vol_s}{need} | RSI={rsi_s} ADX={adx_s} | SMA50={sma_s} | {tail}"
        )
    if emit:
        emit(line)


def _yahoo_as_of_date(df: dict[str, Any] | None) -> str | None:
    if not df:
        return None
    timestamps = df.get("timestamp") or []
    if not timestamps:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(timestamps[-1]), tz=IST).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _build_stock_analysis_record(
    *,
    run_id: str,
    scan_date: str,
    ticker: str,
    tier_name: str,
    df: dict[str, Any] | None,
    fetch_err: str | None,
    fail_reason: str | None,
    passed: bool,
    eval_result: dict[str, Any] | None = None,
    latest_price: float | None = None,
    prev_price: float | None = None,
    pct_change: float | None = None,
    vol_mult: float | None = None,
    rsi_val: float | None = None,
    adx_val: float | None = None,
    sma50_last: float | None = None,
    sma_200_last: float | None = None,
    poc: float | None = None,
    vol_20_avg_last: float | None = None,
    latest_volume: float | None = None,
    sl_price: float | None = None,
    target_price: float | None = None,
    target_gain: float | None = None,
    breeze_stock_code: str | None = None,
) -> dict[str, Any]:
    filt = FILTERS[tier_name]
    bar_count = len(df["close"]) if df and df.get("close") else None
    pass_paths = (eval_result or {}).get("pass_paths") or []
    pass_paths_s = ", ".join(pass_paths) if pass_paths else None
    signal_tier = (eval_result or {}).get("signal_tier")
    sym = ticker.replace(".NS", "").strip()
    return build_analysis_record(
        run_id=run_id,
        scan_date=scan_date,
        ticker=ticker,
        tier=filt["type"],
        symbol_yahoo=ticker,
        fetch_error=fetch_err,
        bar_count=bar_count,
        latest_close=round(latest_price, 2) if latest_price is not None else None,
        prev_close=round(prev_price, 2) if prev_price is not None else None,
        pct_change=round(pct_change, 4) if pct_change is not None else None,
        latest_volume=latest_volume,
        vol_20_avg=round(vol_20_avg_last, 2) if vol_20_avg_last is not None else None,
        vol_mult=round(vol_mult, 4) if vol_mult is not None else None,
        rsi_14=round(rsi_val, 2) if rsi_val is not None else None,
        adx_14=round(adx_val, 2) if adx_val is not None else None,
        sma_50=round(sma50_last, 2) if sma50_last is not None else None,
        sma_200=round(sma_200_last, 2) if sma_200_last is not None else None,
        poc_30d=poc,
        min_price_threshold=filt["min_price"],
        vol_mult_threshold=filt["vol_mult"],
        price_above_sma50=(
            latest_price >= sma50_last
            if latest_price is not None and sma50_last is not None and sma50_last > 0
            else None
        ),
        yahoo_as_of_date=_yahoo_as_of_date(df),
        passed=passed,
        fail_reason=fail_reason,
        entry_low=round(latest_price * 0.985, 2) if passed and latest_price is not None else None,
        entry_high=round(latest_price * 1.01, 2) if passed and latest_price is not None else None,
        stop_loss=round(sl_price, 2) if sl_price is not None else None,
        target_price=round(target_price, 2) if target_price is not None else None,
        target_gain_pct=round(target_gain, 4) if target_gain is not None else None,
        signal_tier=signal_tier,
        persistence_score=(eval_result or {}).get("persistence_score"),
        composite_rank=(eval_result or {}).get("composite_rank"),
        liquidity_quality=(eval_result or {}).get("liquidity_quality"),
        breakout_stage=(eval_result or {}).get("breakout_stage"),
        base_score=(eval_result or {}).get("base_score"),
        pass_paths=pass_paths_s,
        risk_flags=(eval_result or {}).get("risk_flags") or None,
        breeze_stock_code=breeze_stock_code or sym,
        setup_trigger_price=(eval_result or {}).get("setup_trigger_price"),
        setup_rank=(eval_result or {}).get("setup_rank"),
    )


def _build_setup_payload(
    *,
    ticker: str,
    setup_result: dict[str, Any],
    tier_label: str,
) -> str:
    sym = ticker.replace(".NS", "")
    trigger = setup_result.get("setup_trigger_price")
    vol_thresh = setup_result.get("setup_trigger_vol_mult")
    pct_min = setup_result.get("setup_trigger_pct_min", 3.0)
    return f"""**SETUP WATCHLIST DATA: {sym}**
Universe: {tier_label}
Latest Market Price: {setup_result.get('latest_price')}
Daily Change: {setup_result.get('pct_change')}%
Relative Volume: {setup_result.get('vol_mult')}x
Base Score: {setup_result.get('base_score')}
Pivot Proximity: {setup_result.get('pivot_proximity')}
Persistence: {setup_result.get('persistence_score')}/4
Setup Rank: {setup_result.get('setup_rank')}

Trigger (no trade until confirmed):
* Close >= {trigger} or High >= {trigger} with volume >= {vol_thresh}x and day >= +{pct_min}%
* After trigger, re-run breakout scan for PASS confirmation before sizing.
"""


def _build_setup_candidate(
    *,
    ticker: str,
    tier_name: str,
    setup_result: dict[str, Any],
    breeze_stock_code: str | None,
) -> dict[str, Any]:
    filt = FILTERS[tier_name]
    sym = ticker.replace(".NS", "")
    trigger = setup_result.get("setup_trigger_price")
    trigger_s = f">={trigger}" if trigger is not None else "—"
    return {
        "Ticker": sym,
        "Breeze Code": breeze_stock_code or sym,
        "Tier": filt["type"],
        "Price": setup_result.get("latest_price"),
        "Change": _format_change_arrow(float(setup_result.get("pct_change") or 0.0)),
        "Volume Mult": f"{round(float(setup_result.get('vol_mult') or 0.0), 2)}x",
        "RSI": setup_result.get("rsi_val"),
        "ADX": setup_result.get("adx_val"),
        "Entry Range": "—",
        "Est. Stop-Loss": "—",
        "Est. Target (1:2)": "—",
        "Est. Gain": "—",
        "Risk Flags": setup_result.get("risk_flags") or "",
        "Signal Tier": SIGNAL_TIER_PRE_BREAKOUT,
        "Liquidity Quality": setup_result.get("liquidity_quality"),
        "Persistence Score": setup_result.get("persistence_score"),
        "Breakout Stage": setup_result.get("breakout_stage"),
        "Base Score": setup_result.get("base_score"),
        "Composite Rank": setup_result.get("setup_rank"),
        "Setup Rank": setup_result.get("setup_rank"),
        "Pivot Proximity": setup_result.get("pivot_proximity"),
        "Setup Trigger": trigger_s,
        "Pass Paths": "",
        "Watch Reason": "",
        "Payload": _build_setup_payload(
            ticker=ticker,
            setup_result=setup_result,
            tier_label=filt["type"],
        ),
    }


def _cap_setup_candidates_per_tier(
    rows: list[dict[str, Any]],
    *,
    cap: int = SETUP_CAP_PER_TIER,
) -> list[dict[str, Any]]:
    """Keep top ``cap`` PRE_BREAKOUT rows per universe tier by setup rank."""
    non_setup = [r for r in rows if r.get("Signal Tier") != SIGNAL_TIER_PRE_BREAKOUT]
    setup_rows = [r for r in rows if r.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT]
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for row in setup_rows:
        by_tier.setdefault(row.get("Tier") or "", []).append(row)
    capped_setup: list[dict[str, Any]] = []
    for tier_rows in by_tier.values():
        tier_rows.sort(
            key=lambda r: (r.get("Setup Rank") is None, -(float(r.get("Setup Rank") or 0.0))),
        )
        capped_setup.extend(tier_rows[:cap])
    return non_setup + capped_setup


def evaluate_and_audit_stock(
    ticker,
    tier_name,
    emit=None,
    *,
    run_id: str = "",
    scan_date: str = "",
    breeze_stock_code: str | None = None,
    bhav_turnover_lacs: float | None = None,
    delivery_pct: float | None = None,
    avg_delivery_pct: float | None = None,
    free_float_pct: float | None = None,
    sector_lead: float | None = None,
    market_regime: str | None = None,
    days_to_next_earnings: int | None = None,
    benchmark_5d_return: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    filt = FILTERS[tier_name]
    vol_thresh = filt["vol_mult"]
    sym = ticker.replace(".NS", "").strip()
    breeze_code = breeze_stock_code or sym
    df, fetch_err = fetch_yahoo_data(ticker)
    if not df or len(df["close"]) < 50:
        if fetch_err:
            fail_reason = f"insufficient_data ({fetch_err})"
        elif df:
            fail_reason = f"insufficient_data (only {len(df['close'])} bars)"
        else:
            fail_reason = "insufficient_data"
        _emit_stock_diagnostic(emit, ticker, tier_name, fail_reason=fail_reason)
        analysis_record = _build_stock_analysis_record(
            run_id=run_id,
            scan_date=scan_date,
            ticker=ticker,
            tier_name=tier_name,
            df=df,
            fetch_err=fetch_err,
            fail_reason=fail_reason,
            passed=False,
            breeze_stock_code=breeze_code,
        )
        return None, analysis_record

    as_of_idx = len(df["close"]) - 1
    eval_result = evaluate_bars_as_of(
        df,
        as_of_idx,
        tier_name,
        bhav_turnover_lacs=bhav_turnover_lacs,
        delivery_pct=delivery_pct,
        avg_delivery_pct=avg_delivery_pct,
        free_float_pct=free_float_pct,
        sector_lead=sector_lead,
        market_regime=market_regime,
        days_to_next_earnings=days_to_next_earnings,
        benchmark_5d_return=benchmark_5d_return,
    )
    latest_price = eval_result["latest_price"]
    prev_price = eval_result["prev_price"]
    pct_change = eval_result["pct_change"]
    vol_mult = eval_result["vol_mult"]
    rsi_val = eval_result["rsi_val"]
    adx_val = eval_result["adx_val"]
    sma50_last = eval_result["sma50_last"]
    vol_20_avg_last = eval_result["vol_20_avg_last"]
    latest_volume = eval_result["latest_volume"]
    sl_price = eval_result["sl_price"]
    target_price = eval_result["target_price"]
    target_gain = eval_result["target_gain"]
    risk_flags = eval_result["risk_flags"]
    sma_200_last = eval_result.get("sma_200_last")
    poc = eval_result["poc"]

    common_metrics = {
        "latest_price": latest_price,
        "prev_price": prev_price,
        "pct_change": pct_change,
        "vol_mult": vol_mult,
        "rsi_val": rsi_val,
        "adx_val": adx_val,
        "sma50_last": sma50_last,
        "sma_200_last": sma_200_last,
        "poc": poc,
        "vol_20_avg_last": vol_20_avg_last,
        "latest_volume": latest_volume,
        "sl_price": sl_price,
        "target_price": target_price,
        "target_gain": target_gain,
    }

    fail_reason = eval_result.get("fail_reason")
    signal_tier = eval_result.get("signal_tier")
    setup_result: dict[str, Any] | None = None
    if fail_reason and signal_tier not in (SIGNAL_TIER_PASS, SIGNAL_TIER_WATCH):
        setup_result = evaluate_setup_as_of(
            df,
            as_of_idx,
            tier_name,
            min_price=filt["min_price"],
            vol_mult_threshold=vol_thresh,
            bhav_turnover_lacs=bhav_turnover_lacs,
            delivery_pct=delivery_pct,
            free_float_pct=free_float_pct,
            sector_lead=sector_lead,
            rsi_val=eval_result.get("rsi_val"),
            adx_val=eval_result.get("adx_val"),
            pct_change=eval_result.get("pct_change"),
            vol_mult=eval_result.get("vol_mult"),
            sma20_last=eval_result.get("sma20_last"),
            sma50_last=eval_result.get("sma50_last"),
        )

    record_eval = eval_result if setup_result is None else {**eval_result, **setup_result}

    if fail_reason and setup_result is None:
        _emit_stock_diagnostic(
            emit, ticker, tier_name, latest_price, pct_change,
            vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
            status="FAIL", fail_reason=fail_reason,
        )
        analysis_record = _build_stock_analysis_record(
            run_id=run_id,
            scan_date=scan_date,
            ticker=ticker,
            tier_name=tier_name,
            df=df,
            fetch_err=fetch_err,
            fail_reason=fail_reason,
            passed=False,
            eval_result=record_eval,
            breeze_stock_code=breeze_code,
            **common_metrics,
        )
        return None, analysis_record

    if setup_result is not None:
        _emit_stock_diagnostic(
            emit, ticker, tier_name, latest_price, pct_change,
            vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
            status=SIGNAL_TIER_PRE_BREAKOUT, fail_reason="",
        )
        candidate = _build_setup_candidate(
            ticker=ticker,
            tier_name=tier_name,
            setup_result=setup_result,
            breeze_stock_code=breeze_code,
        )
        analysis_record = _build_stock_analysis_record(
            run_id=run_id,
            scan_date=scan_date,
            ticker=ticker,
            tier_name=tier_name,
            df=df,
            fetch_err=fetch_err,
            fail_reason=None,
            passed=False,
            eval_result=record_eval,
            breeze_stock_code=breeze_code,
            **common_metrics,
        )
        return candidate, analysis_record

    status = signal_tier or "PASS"
    _emit_stock_diagnostic(
        emit, ticker, tier_name, latest_price, pct_change,
        vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
        status=status, fail_reason="",
    )

    sma_200_last = eval_result.get("sma_200_last") or 0.0

    payload = f"""**TECHNICAL GROUNDING DATA: {ticker.replace(".NS", "")}**
Data Retrieval Timestamp: {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} (IST)
Latest Market Price: {round(latest_price, 2)} ({'+' if pct_change >= 0 else ''}{round(pct_change, 2)}% today)
Relative Volume (20-day Average): {round(vol_mult, 2)}x
Relative Strength Index (RSI 14): {round(rsi_val, 2)}
Average Directional Index (ADX 14): {round(adx_val, 2)}

Trend Indicators:
* 50-period SMA: {round(sma50_last, 2)} (Price is {'ABOVE' if latest_price > sma50_last else 'BELOW'} 50 SMA)
* 200-period SMA: {round(sma_200_last, 2)} (Price is {'ABOVE' if sma_200_last and latest_price > sma_200_last else 'BELOW'} 200 SMA)

Volume Profile (30-day Visible Range):
* Point of Control (POC): {round(poc, 2)}
"""

    pass_paths = eval_result.get("pass_paths") or []
    watch_reason = _derive_watch_reason(eval_result)

    candidate = {
        "Ticker": sym,
        "Breeze Code": breeze_code,
        "Tier": filt["type"],
        "Price": round(latest_price, 2),
        "Change": _format_change_arrow(pct_change),
        "Volume Mult": f"{round(vol_mult, 2)}x",
        "RSI": rsi_val,
        "ADX": adx_val,
        "Entry Range": f"{round(latest_price * 0.985, 2)} - {round(latest_price * 1.01, 2)}",
        "Est. Stop-Loss": round(sl_price, 2),
        "Est. Target (1:2)": round(target_price, 2),
        "Est. Gain": f"{round(target_gain, 2)}%",
        "Risk Flags": risk_flags,
        "Signal Tier": signal_tier or SIGNAL_TIER_PASS,
        "Liquidity Quality": eval_result.get("liquidity_quality"),
        "Persistence Score": eval_result.get("persistence_score"),
        "Breakout Stage": eval_result.get("breakout_stage"),
        "Base Score": eval_result.get("base_score"),
        "Composite Rank": eval_result.get("composite_rank"),
        "Sector Lead": sector_lead,
        "Delivery Pct": eval_result.get("delivery_pct"),
        "VPR": eval_result.get("vpr"),
        "CMF": eval_result.get("cmf"),
        "Pass Paths": ", ".join(pass_paths) if pass_paths else "",
        "Watch Reason": watch_reason or "",
        "Payload": payload,
    }
    passed = bool(eval_result.get("passed"))
    analysis_record = _build_stock_analysis_record(
        run_id=run_id,
        scan_date=scan_date,
        ticker=ticker,
        tier_name=tier_name,
        df=df,
        fetch_err=fetch_err,
        fail_reason=None,
        passed=passed,
        eval_result=eval_result,
        breeze_stock_code=breeze_code,
        **common_metrics,
    )
    return candidate, analysis_record


_BREAKOUT_STAGE_LABELS: dict[int, str] = {
    1: "Stage 1 Fresh",
    2: "Stage 2 Young",
    3: "Stage 3 Parabolic",
}


def _format_change_arrow(pct_change: float | str) -> str:
    """Format daily change with unicode direction cue (▲ up, ▼ down, ● flat)."""
    if isinstance(pct_change, str):
        raw = pct_change.lstrip("▲▼● ").lstrip("+").rstrip("%").strip()
        try:
            value = float(raw)
        except ValueError:
            return pct_change
    else:
        value = float(pct_change)
    arrow = "▲" if value > 0 else ("▼" if value < 0 else "●")
    sign = "+" if value > 0 else ""
    return f"{arrow} {sign}{value:.2f}%"


def _format_breakout_stage(stage: int | None) -> str:
    if stage is None:
        return "—"
    label = _BREAKOUT_STAGE_LABELS.get(int(stage), f"Stage {stage}")
    return f"{int(stage)} — {label}"


def _format_signal_tier_badge(tier: str | None) -> str:
    if tier == SIGNAL_TIER_PASS:
        return "🟢 PASS"
    if tier == SIGNAL_TIER_WATCH:
        return "🟡 WATCH"
    if tier == SIGNAL_TIER_PRE_BREAKOUT:
        return "🟠 SETUP"
    return tier or "—"


def _derive_watch_reason(eval_result: dict[str, Any]) -> str | None:
    reason = eval_result.get("v7_watch_reason")
    if reason:
        return str(reason)
    if eval_result.get("adx_soft_solo_watch"):
        return "v6_adx_soft_solo"
    if (
        eval_result.get("signal_tier") == SIGNAL_TIER_WATCH
        and "power_gap" in (eval_result.get("pass_paths") or [])
    ):
        pg = eval_result.get("power_gap_confirmation") or {}
        if pg.get("power_gap_confirmation_required") and not pg.get("confirmed"):
            return "v6_power_gap_unconfirmed"
    return None


def _format_v7_summary_line(row: dict[str, Any]) -> str:
    tier = row.get("Signal Tier") or "—"
    stage = _format_breakout_stage(row.get("Breakout Stage"))
    persist = row.get("Persistence Score")
    persist_s = f"{persist}/4" if persist is not None else "—"
    lq = row.get("Liquidity Quality")
    lq_s = f"{lq:.1f}" if isinstance(lq, (int, float)) else (str(lq) if lq is not None else "—")
    rank = row.get("Composite Rank")
    rank_s = f"{rank:.1f}" if isinstance(rank, (int, float)) else (str(rank) if rank is not None else "—")
    paths = row.get("Pass Paths") or ""
    parts = [
        f"Signal {_format_signal_tier_badge(tier)}",
        stage,
        f"Persistence {persist_s}",
        f"LQ {lq_s}",
        f"Rank {rank_s}",
    ]
    if paths:
        parts.append(f"Paths: {paths}")
    return " · ".join(parts)


def _display_num(value: Any, *, decimals: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{float(value):.{decimals}f}"
    return str(value)


_NA_FLOAT_DISPLAY = frozenset({"—", "-", "", "n/a", "na", "none"})


def _optional_float_from_display(raw: Any) -> float | None:
    """Parse a display string to float; treat em dash and placeholders as None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return None if val != val else val
    s = str(raw).strip()
    if s.casefold() in _NA_FLOAT_DISPLAY:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return None if val != val else val


def _sort_report_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PASS first, then WATCH, then PRE_BREAKOUT; each group by rank descending."""

    def _tier_rank(tier: str | None) -> int:
        if tier == SIGNAL_TIER_PASS:
            return 0
        if tier == SIGNAL_TIER_WATCH:
            return 1
        if tier == SIGNAL_TIER_PRE_BREAKOUT:
            return 2
        return 3

    def _key(row: dict[str, Any]) -> tuple[int, float]:
        tier_rank = _tier_rank(row.get("Signal Tier"))
        if row.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT:
            rank_val = float(row.get("Setup Rank") or row.get("Composite Rank") or 0.0)
        else:
            composite = row.get("Composite Rank")
            rank_val = float(composite) if isinstance(composite, (int, float)) else 0.0
        return (tier_rank, -rank_val)

    return sorted(rows, key=_key)


def serialize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a scanner row for JSON API responses."""
    change_raw = str(row.get("Change", "")).lstrip("▲▼● ").lstrip("+").rstrip("%").strip()
    vol_raw = str(row.get("Volume Mult", "")).rstrip("x").strip()
    gain_raw = str(row.get("Est. Gain", "")).rstrip("%").strip()
    return {
        "ticker": row["Ticker"],
        "breeze_stock_code": row.get("Breeze Code"),
        "tier": row["Tier"],
        "price": row["Price"],
        "change_pct": _optional_float_from_display(change_raw),
        "change_display": row["Change"],
        "volume_mult": _optional_float_from_display(vol_raw),
        "volume_mult_display": row["Volume Mult"],
        "rsi": row["RSI"],
        "adx": row["ADX"],
        "entry_range": row["Entry Range"],
        "est_stop_loss": row["Est. Stop-Loss"],
        "est_target": row["Est. Target (1:2)"],
        "est_gain_pct": _optional_float_from_display(gain_raw),
        "est_gain_display": row["Est. Gain"],
        "risk_flags": row["Risk Flags"],
        "signal_tier": row.get("Signal Tier"),
        "setup_rank": row.get("Setup Rank"),
        "setup_trigger": row.get("Setup Trigger"),
        "pivot_proximity": row.get("Pivot Proximity"),
        "liquidity_quality": row.get("Liquidity Quality"),
        "persistence_score": row.get("Persistence Score"),
        "breakout_stage": row.get("Breakout Stage"),
        "base_score": row.get("Base Score"),
        "composite_rank": row.get("Composite Rank"),
        "pass_paths": row.get("Pass Paths") or None,
        "watch_reason": row.get("Watch Reason") or None,
        "payload": row["Payload"],
    }


def _build_report_markdown(all_results: list[dict[str, Any]], scan_date: datetime.date) -> str:
    if not all_results:
        return f"""# Consolidated Daily Breakout Report ({scan_date.strftime('%Y-%m-%d')})
No small-cap or micro-cap stocks met the strict price-volume breakout and trend strength (ADX > 25) filters today.
This is typical during consolidative, bearish, or highly volatile market days.
"""

    tiers_data = {
        "Small-Cap (Nifty Smallcap 100)": [],
        "Micro-Cap (Nifty Microcap 250)": [],
    }
    for row in all_results:
        tiers_data[row["Tier"]].append(row)

    tables_md = ""
    detailed_payloads_md = "\n## Detailed Technical Grounding Payloads (Copy-Paste to Gemini)\n"
    detailed_payloads_md += (
        "Use the copy-pasteable data blocks below to feed directly into your Gemini Stock Analyst "
        "prompt to generate swing trading playbooks.\n\n"
    )

    for tier_name, rows in tiers_data.items():
        breakout_rows = [r for r in rows if r.get("Signal Tier") != SIGNAL_TIER_PRE_BREAKOUT]
        setup_rows = [r for r in rows if r.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT]

        tables_md += f"\n### {tier_name} Breakouts\n"
        if not breakout_rows:
            tables_md += "No breakout PASS/WATCH stocks in this tier today.\n"
        else:
            sorted_rows = _sort_report_candidates(breakout_rows)
            tables_md += (
                "| Ticker | Breeze | Signal Tier | Breakout Stage | Persistence | LQ | Base | Rank | "
                "Price | Change | Vol | RSI | ADX | Entry | Stop | Target | Gain | Pass Paths | Risk Flags |\n"
            )
            tables_md += (
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | "
                ":--- | :--- | :--- | :--- | :--- | :--- |\n"
            )
            for r in sorted_rows:
                tables_md += (
                    f"| {r['Ticker']} | {r.get('Breeze Code') or '—'} | "
                    f"{_format_signal_tier_badge(r.get('Signal Tier'))} | "
                    f"{_format_breakout_stage(r.get('Breakout Stage'))} | "
                    f"{r.get('Persistence Score', '—')}/4 | "
                    f"{_display_num(r.get('Liquidity Quality'))} | "
                    f"{_display_num(r.get('Base Score'))} | "
                    f"{_display_num(r.get('Composite Rank'))} | "
                    f"{r['Price']} | {r['Change']} | {r['Volume Mult']} | "
                    f"{r['RSI']} | {r['ADX']} | {r['Entry Range']} | {r['Est. Stop-Loss']} | "
                    f"{r['Est. Target (1:2)']} | {r['Est. Gain']} | "
                    f"{r.get('Pass Paths') or '—'} | {r['Risk Flags']} |\n"
                )

        tables_md += f"\n### {tier_name} — Setup Watchlist (PRE_BREAKOUT)\n"
        if not setup_rows:
            tables_md += "No coiling setups in this tier today.\n"
        else:
            sorted_setup = _sort_report_candidates(setup_rows)
            tables_md += (
                "| Ticker | Breeze | Base | Pivot Prox | Vol | Persistence | Setup Rank | Trigger | Risk |\n"
            )
            tables_md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            for r in sorted_setup:
                tables_md += (
                    f"| {r['Ticker']} | {r.get('Breeze Code') or '—'} | "
                    f"{_display_num(r.get('Base Score'))} | "
                    f"{_display_num(r.get('Pivot Proximity'))} | {r['Volume Mult']} | "
                    f"{r.get('Persistence Score', '—')}/4 | "
                    f"{_display_num(r.get('Setup Rank'))} | {r.get('Setup Trigger') or '—'} | "
                    f"{r['Risk Flags']} |\n"
                )

        for r in _sort_report_candidates(rows):
            detailed_payloads_md += f"### {r['Ticker']}\n"
            detailed_payloads_md += f"**v7:** {_format_v7_summary_line(r)}\n"
            if r.get("Watch Reason"):
                detailed_payloads_md += f"**WATCH reason:** {r['Watch Reason']}\n"
            detailed_payloads_md += "\n```markdown\n"
            detailed_payloads_md += r["Payload"]
            detailed_payloads_md += "```\n\n"

    return f"""# Consolidated Daily Breakout & Technical Grounding Report ({scan_date.strftime('%Y-%m-%d')})
This consolidated report identifies high-probability daily breakouts exclusively in the Nifty Smallcap 100 and Nifty Microcap 250 universes, and automatically performs the detailed technical audit for each discovered stock.

## MICRO-CAP TRADING WARNING
Small and micro-cap stocks are highly prone to daily circuit locks. In the event of a lower circuit lock, your technical Stop-Loss will not execute due to a complete lack of buyers, exposing you to severe capital slippage. Limit exposure to maximum 1-2% of swing capital per trade and verify that no selected stock is currently placed under SEBI's GSM/ASM Lists.

## Executive Summary Matrix
{tables_md}
{detailed_payloads_md}

## Next Steps
Copy the technical grounding data block for your ticker of interest from the section above, and feed it along with its chart screenshot into your Gemini Stock Analyst prompt to generate a complete, mathematically backed Swing Trading Playbook.

## Compliance Disclaimer
This analysis is for educational and informational purposes only. It does not constitute registered investment advice or a personal recommendation. Indian stock market investing involves high risk. Please consult a SEBI-registered financial advisor before making any investment decisions.
"""


def _format_top_candidates_table(rows: list[dict[str, Any]], *, limit: int = 15) -> str:
    if not rows:
        return ""
    lines = [
        "| Ticker | Tier | Price | Change | Vol | Signal Tier | Stage | Persist | Rank |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in _sort_report_candidates(rows)[:limit]:
        persist = row.get("Persistence Score")
        persist_s = f"{persist}/4" if persist is not None else "—"
        lines.append(
            f"| {row['Ticker']} | {row['Tier']} | {row['Price']} | {row['Change']} | "
            f"{row['Volume Mult']} | {_format_signal_tier_badge(row.get('Signal Tier'))} | "
            f"{_format_breakout_stage(row.get('Breakout Stage'))} | {persist_s} | "
            f"{_display_num(row.get('Composite Rank'))} |"
        )
    return "\n".join(lines)


def _format_tier_summary_table(rows: list[dict[str, Any]], *, limit: int = 10) -> str:
    if not rows:
        return "_None in this tier._"
    return _format_top_candidates_table(rows, limit=limit)


def _build_breakout_email_html(
    *,
    scan_date: datetime.date,
    tickers_scanned: int,
    all_results: list[dict[str, Any]],
) -> str:
    from html import escape

    pass_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_PASS]
    watch_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_WATCH]
    setup_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT]
    sorted_all = _sort_report_candidates(all_results)

    def _tier_badge(tier: str | None) -> str:
        if tier == SIGNAL_TIER_PASS:
            return (
                '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                'background:#34a853;color:#fff;font-weight:700;font-size:11px;">PASS</span>'
            )
        if tier == SIGNAL_TIER_WATCH:
            return (
                '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                'background:#f9ab00;color:#fff;font-weight:700;font-size:11px;">WATCH</span>'
            )
        if tier == SIGNAL_TIER_PRE_BREAKOUT:
            return (
                '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                'background:#e8710a;color:#fff;font-weight:700;font-size:11px;">SETUP</span>'
            )
        return escape(tier or "—")

    def _stage_badge(stage: int | None) -> str:
        colors = {1: "#34a853", 2: "#4285f4", 3: "#ea4335"}
        if stage is None:
            return "—"
        color = colors.get(int(stage), "#5f6368")
        label = _BREAKOUT_STAGE_LABELS.get(int(stage), f"Stage {stage}")
        return (
            f'<span style="color:{color};font-weight:700;font-size:11px;">'
            f"S{int(stage)} {escape(label.split(' ', 1)[-1])}</span>"
        )

    def _change_cell(change: str) -> str:
        text = str(change)
        color = "#34a853" if "▲" in text else ("#ea4335" if "▼" in text else "#5f6368")
        return f'<span style="color:{color};font-weight:600;">{escape(text)}</span>'

    def _candidate_table(rows: list[dict[str, Any]], *, limit: int = 10) -> str:
        if not rows:
            return '<p style="margin:0;color:#5f6368;font-size:12px;">None.</p>'
        thead = (
            "<tr>"
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Ticker</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Tier</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Signal</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Stage</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Persist</th>'
            '<th style="text-align:right;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Rank</th>'
            '<th style="text-align:right;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Price</th>'
            '<th style="text-align:right;padding:6px 8px;border-bottom:2px solid #ddd;font-size:11px;">Change</th>'
            "</tr>"
        )
        body_rows = []
        for row in _sort_report_candidates(rows)[:limit]:
            persist = row.get("Persistence Score")
            persist_s = f"{persist}/4" if persist is not None else "—"
            body_rows.append(
                "<tr>"
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-weight:600;">{escape(row["Ticker"])}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:11px;">{escape(row["Tier"])}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{_tier_badge(row.get("Signal Tier"))}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{_stage_badge(row.get("Breakout Stage"))}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{escape(persist_s)}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;">{escape(_display_num(row.get("Composite Rank")))}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;">{row["Price"]}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;">{_change_cell(row["Change"])}</td>'
                "</tr>"
            )
        return (
            '<table style="width:100%;border-collapse:collapse;margin:0 0 12px;">'
            f"<thead>{thead}</thead><tbody>{''.join(body_rows)}</tbody></table>"
        )

    summary = (
        f"<p style=\"margin:0 0 8px;\"><strong>Tickers scanned:</strong> {tickers_scanned}</p>"
        f"<p style=\"margin:0 0 12px;\"><strong>Candidates:</strong> {len(all_results)} total "
        f"({len(pass_rows)} PASS, {len(watch_rows)} WATCH, {len(setup_rows)} SETUP)</p>"
    )
    pass_section = (
        '<div style="margin:0 0 16px;">'
        f'<h3 style="margin:0 0 8px;color:#34a853;font-size:14px;">PASS ({len(pass_rows)})</h3>'
        f"{_candidate_table(pass_rows)}</div>"
    )
    watch_section = (
        '<div style="margin:0 0 16px;">'
        f'<h3 style="margin:0 0 8px;color:#f9ab00;font-size:14px;">WATCH ({len(watch_rows)})</h3>'
        f"{_candidate_table(watch_rows)}</div>"
    )
    setup_section = (
        '<div style="margin:0 0 16px;">'
        f'<h3 style="margin:0 0 8px;color:#e8710a;font-size:14px;">SETUP ({len(setup_rows)})</h3>'
        f"{_candidate_table(setup_rows)}</div>"
    )
    top_section = ""
    if sorted_all:
        top_section = (
            '<div style="margin:0 0 16px;">'
            '<h3 style="margin:0 0 8px;color:#4285f4;font-size:14px;">Top candidates (all tiers)</h3>'
            f"{_candidate_table(sorted_all, limit=15)}</div>"
        )

    return (
        "<html><body style=\"margin:0;padding:16px;background:#f8f9fa;color:#202124;"
        "font-family:Arial,sans-serif;\">"
        f'<h2 style="margin:0 0 6px;">Titan breakout scan — {escape(scan_date.isoformat())}</h2>'
        f"{summary}{pass_section}{watch_section}{setup_section}{top_section}"
        '<p style="color:#5f6368;font-size:12px;margin-top:12px;">Full report in plain-text part. '
        "Generated by Titan V12.0</p>"
        "</body></html>"
    )


def _build_breakout_email_body(
    *,
    scan_date: datetime.date,
    tickers_scanned: int,
    all_results: list[dict[str, Any]],
    report_markdown: str | None,
) -> str:
    pass_count = sum(1 for row in all_results if row.get("Signal Tier") == SIGNAL_TIER_PASS)
    watch_count = sum(1 for row in all_results if row.get("Signal Tier") == SIGNAL_TIER_WATCH)
    setup_count = sum(1 for row in all_results if row.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT)
    candidate_count = len(all_results)
    has_signal_tier = any("Signal Tier" in row for row in all_results)

    lines = [
        f"Titan breakout scan — {scan_date.isoformat()}",
        "",
        f"Tickers scanned: {tickers_scanned}",
    ]
    if has_signal_tier:
        lines.append(
            f"Candidates: {candidate_count} total "
            f"({pass_count} PASS, {watch_count} WATCH, {setup_count} SETUP)"
        )
    else:
        lines.append(f"Candidates: {candidate_count}")

    if candidate_count == 0:
        lines.extend(["", "No breakouts today."])
    else:
        pass_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_PASS]
        watch_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_WATCH]
        if pass_rows:
            lines.extend(["", f"## PASS candidates ({len(pass_rows)})", ""])
            lines.append(_format_tier_summary_table(pass_rows))
        if watch_rows:
            lines.extend(["", f"## WATCH candidates ({len(watch_rows)})", ""])
            lines.append(_format_tier_summary_table(watch_rows))
        setup_rows = [r for r in all_results if r.get("Signal Tier") == SIGNAL_TIER_PRE_BREAKOUT]
        if setup_rows:
            lines.extend(["", f"## SETUP candidates ({len(setup_rows)})", ""])
            lines.append(_format_tier_summary_table(setup_rows))
        lines.extend(["", "## Top candidates (all tiers)", ""])
        lines.append(_format_top_candidates_table(all_results))
        if report_markdown:
            lines.extend(["", report_markdown])

    return "\n".join(lines).strip()


def _send_breakout_success_email(
    *,
    scan_date: datetime.date,
    tickers_scanned: int,
    all_results: list[dict[str, Any]],
    report_markdown: str | None,
) -> tuple[bool, str]:
    try:
        from .email_notify import (
            _smtp_config,
            mask_email_recipients,
            send_success_post_email,
            smtp_not_configured_reason,
        )
    except ImportError:
        from email_notify import (
            _smtp_config,
            mask_email_recipients,
            send_success_post_email,
            smtp_not_configured_reason,
        )

    skip_reason = smtp_not_configured_reason()
    if skip_reason:
        status = f"Breakout email: NOT SENT — {skip_reason}"
        print(status, flush=True)
        return False, status

    cfg = _smtp_config()
    assert cfg is not None
    masked_to = mask_email_recipients(cfg["to"])  # type: ignore[arg-type]

    body = _build_breakout_email_body(
        scan_date=scan_date,
        tickers_scanned=tickers_scanned,
        all_results=all_results,
        report_markdown=report_markdown,
    )
    html_body = _build_breakout_email_html(
        scan_date=scan_date,
        tickers_scanned=tickers_scanned,
        all_results=all_results,
    )
    emailed_ok = send_success_post_email(
        body,
        subject_prefix="Titan breakout scan",
        eod_as_of_date=scan_date.isoformat(),
        html_body=html_body,
    )
    if emailed_ok:
        status = f"Breakout email: SENT to {masked_to}"
    else:
        status = "Breakout email: NOT SENT — SMTP send failed (see stderr for details)"
    print(status, flush=True)
    return emailed_ok, status


def _send_breakout_failure_email(exc: BaseException) -> bool:
    try:
        from .email_notify import send_failure_email
    except ImportError:
        from email_notify import send_failure_email

    summary = str(exc).strip().split("\n", 1)[0].strip()
    if len(summary) > 180:
        summary = summary[:177] + "..."
    emailed_ok = send_failure_email(
        f"[Breakout scan] {summary}",
        detail=traceback.format_exc(),
        subject_prefix="Titan breakout scan",
    )
    if emailed_ok:
        logger.info("Breakout scan failure email sent.")
    else:
        logger.info("Breakout scan failure email skipped (SMTP not configured).")
    return emailed_ok


def run_breakout_scan(
    output_dir: Path | str | None = None,
    *,
    write_report: bool = True,
    emit_to_stdout: bool = True,
) -> dict[str, Any]:
    """Run the full breakout scan and return structured results for API/CLI callers."""
    global _OUTPUT_DIR
    started_at = datetime.datetime.now()
    scan_date = started_at.date()
    out_dir = resolve_output_dir(output_dir)
    _OUTPUT_DIR = out_dir

    log_path = out_dir / "breakout_scanner_run.log"
    report_path = out_dir / "daily_breakout_report_v2.md"
    log_lines: list[str] = []

    def emit_line(line: str) -> None:
        log_lines.append(line)
        if emit_to_stdout:
            print(line, flush=True)

    if emit_to_stdout:
        print("=========================================================================")
        print("        NSE CONSOLIDATED SMALL & MICRO CAP SCANNER & AUDITOR")
        print(" Universes: Nifty Smallcap 100, Nifty Microcap 250")
        print(f" Date: {scan_date.strftime('%Y-%m-%d')}")
        print("=========================================================================\n")

    all_results: list[dict[str, Any]] = []
    analysis_records: list[dict[str, Any]] = []
    tier_ticker_counts: dict[str, int] = {}
    scan_ticker_count = 0
    run_id = str(uuid.uuid4())
    scan_date_iso = scan_date.isoformat()

    try:
        with log_path.open("w", encoding="utf-8") as log_file:

            def emit_and_log(line: str) -> None:
                emit_line(line)
                log_file.write(line + "\n")
                log_file.flush()

            emit_and_log(f"=== Breakout scanner run {started_at.isoformat()} run_id={run_id} ===")
            try:
                try:
                    from .breakout_ohlcv_store import clear_bulk_cache, get_ohlcv_stats, reset_ohlcv_stats
                except ImportError:
                    from breakout_ohlcv_store import clear_bulk_cache, get_ohlcv_stats, reset_ohlcv_stats
                reset_ohlcv_stats()
                clear_bulk_cache()
            except Exception:  # noqa: BLE001
                def get_ohlcv_stats() -> dict[str, int]:  # type: ignore[misc]
                    return {"supabase_hits": 0, "yahoo_fetches": 0}

            warm_yahoo_session()
            emit_and_log("Yahoo session warm-up complete.")

            all_tickers: list[str] = []
            tier_ticker_lists: dict[str, list[str]] = {}
            for tier_key, url in INDEX_URLS.items():
                tickers = download_nse_tickers(url)
                tier_ticker_lists[tier_key] = tickers
                all_tickers.extend(tickers)

            all_syms = sorted({t.replace(".NS", "").upper() for t in all_tickers})
            try:
                try:
                    from .breakout_ohlcv_store import load_ohlcv_bulk_from_supabase
                except ImportError:
                    from breakout_ohlcv_store import load_ohlcv_bulk_from_supabase
                bulk = load_ohlcv_bulk_from_supabase(all_syms, min_bars=50, max_stale_trading_days=3)
                if bulk:
                    emit_and_log(f"Supabase OHLCV bulk cache: {len(bulk)}/{len(all_syms)} symbols.")
            except Exception as exc:  # noqa: BLE001
                emit_and_log(f"Supabase OHLCV bulk load skipped: {exc}")

            delivery_by_sym: dict[str, float | None] = {}
            delivery_anomaly_by_sym: dict[str, dict[str, float | None]] = {}
            earnings_days_by_sym: dict[str, int | None] = {}
            sector_lead_by_sym: dict[str, float] = {}
            free_float_by_sym: dict[str, float | None] = {}
            turnover_by_sym: dict[str, float] = {}

            try:
                from breakout_eod_context import (
                    load_bhav_turnover_lacs_by_symbol,
                    load_delivery_anomaly_by_symbol,
                    load_delivery_pct_by_symbol,
                    load_days_to_next_earnings_by_symbol,
                    load_free_float_pct_by_symbol,
                )
            except ImportError:
                from .breakout_eod_context import (
                    load_bhav_turnover_lacs_by_symbol,
                    load_delivery_anomaly_by_symbol,
                    load_delivery_pct_by_symbol,
                    load_days_to_next_earnings_by_symbol,
                    load_free_float_pct_by_symbol,
                )

            try:
                from breakout_sector_context import load_sector_lead_scores
            except ImportError:
                from .breakout_sector_context import load_sector_lead_scores

            bhav_dir = _repo_root() / "temp" / "nse_cache"
            if bhav_dir.is_dir():
                turnover_by_sym = load_bhav_turnover_lacs_by_symbol(bhav_dir)
                emit_and_log(f"Bhav turnover cache: {len(turnover_by_sym)} symbols.")

            try:
                delivery_by_sym = load_delivery_pct_by_symbol(
                    all_syms, as_of_date=scan_date_iso, nse_cache_dir=bhav_dir if bhav_dir.is_dir() else None,
                )
                delivery_anomaly_by_sym = load_delivery_anomaly_by_symbol(
                    all_syms,
                    as_of_date=scan_date_iso,
                    nse_cache_dir=bhav_dir if bhav_dir.is_dir() else None,
                )
                earnings_days_by_sym = load_days_to_next_earnings_by_symbol(
                    all_syms, as_of_date=scan_date_iso,
                )
                sector_lead_by_sym = load_sector_lead_scores(all_syms, as_of_date=scan_date_iso)
                free_float_by_sym = load_free_float_pct_by_symbol(all_syms, as_of_date=scan_date_iso)
                loaded_delivery = sum(1 for v in delivery_by_sym.values() if v is not None)
                loaded_anomaly = sum(
                    1 for ctx in delivery_anomaly_by_sym.values() if ctx.get("delivery_t") is not None
                )
                loaded_earnings = sum(1 for v in earnings_days_by_sym.values() if v is not None)
                emit_and_log(
                    f"EOD context: delivery={loaded_delivery}/{len(all_syms)} "
                    f"delivery_anomaly_t={loaded_anomaly}/{len(all_syms)} "
                    f"earnings_calendar={loaded_earnings}/{len(all_syms)} "
                    f"sector_lead={len(sector_lead_by_sym)} free_float="
                    f"{sum(1 for v in free_float_by_sym.values() if v is not None)}",
                )
            except Exception as exc:  # noqa: BLE001
                emit_and_log(f"EOD context bulk load skipped: {exc}")

            # TODO: When corporate_actions_calendar ingest is unavailable, manually verify
            # earnings dates for any PASS/WATCH breakout candidates before sizing.
            if not any(v is not None for v in earnings_days_by_sym.values()):
                emit_and_log(
                    "Earnings calendar: no upcoming results dates loaded — "
                    "verify earnings manually for PASS/WATCH names."
                )

            breeze_code_by_sym: dict[str, str] = {}
            try:
                cfg = load_config(require_breeze=False, require_gemini=False)
                breeze_code_by_sym = build_breeze_code_map(all_syms, cfg)
                emit_and_log(f"Breeze stock codes resolved: {len(breeze_code_by_sym)} symbols.")
            except ValueError as exc:
                breeze_code_by_sym = build_breeze_code_map(all_syms)
                emit_and_log(f"Breeze codes (scrip master only): {exc}")

            regime_info = evaluate_market_regime()
            market_regime = regime_info.get("market_regime")
            benchmark_5d_return = regime_info.get("benchmark_5d_return")
            emit_and_log(
                f"Market regime: {market_regime} "
                f"({regime_info.get('benchmark_ticker')} "
                f"close={regime_info.get('benchmark_close')} "
                f"sma20={regime_info.get('benchmark_sma20')} "
                f"ret5d={benchmark_5d_return})"
            )
            if regime_info.get("fetch_error") and market_regime == "UNKNOWN":
                emit_and_log(f"Market regime fetch note: {regime_info['fetch_error']}")

            for tier_key, tickers in tier_ticker_lists.items():
                tier_label = FILTERS[tier_key]["type"]
                tier_ticker_counts[tier_label] = len(tickers)
                if emit_to_stdout:
                    print(f"Scanning {tier_label} ({len(tickers)} tickers)...")
                if not tickers:
                    if emit_to_stdout:
                        print(f" Warning: No tickers found for {tier_key}. Skipping.")
                    continue

                total = len(tickers)
                if emit_to_stdout:
                    print(f" Found {total} tickers. Scanning & Auditing technicals...")

                for ticker in tickers:
                    sym = ticker.replace(".NS", "").upper()
                    anomaly_ctx = delivery_anomaly_by_sym.get(sym) or {}
                    delivery_t = anomaly_ctx.get("delivery_t")
                    delivery_for_eval = (
                        float(delivery_t)
                        if delivery_t is not None
                        else delivery_by_sym.get(sym)
                    )
                    candidate, analysis_record = evaluate_and_audit_stock(
                        ticker,
                        tier_key,
                        emit=emit_and_log,
                        run_id=run_id,
                        scan_date=scan_date_iso,
                        breeze_stock_code=breeze_code_by_sym.get(sym),
                        bhav_turnover_lacs=turnover_by_sym.get(sym),
                        delivery_pct=delivery_for_eval,
                        avg_delivery_pct=anomaly_ctx.get("avg_delivery_20d"),
                        free_float_pct=free_float_by_sym.get(sym),
                        sector_lead=sector_lead_by_sym.get(sym),
                        market_regime=market_regime if market_regime != "UNKNOWN" else None,
                        days_to_next_earnings=earnings_days_by_sym.get(sym),
                        benchmark_5d_return=benchmark_5d_return,
                    )
                    analysis_records.append(analysis_record)
                    if candidate:
                        all_results.append(candidate)
                    scan_ticker_count += 1
                    if scan_ticker_count % 50 == 0:
                        msg = f"Chunk cool-down: {scan_ticker_count} tickers scanned, sleeping 120s..."
                        emit_and_log(msg)
                        time.sleep(120)
                if emit_to_stdout:
                    print("\n Scan & Audit complete for this tier.\n")

            all_results = _cap_setup_candidates_per_tier(all_results)
            ohlcv_stats = get_ohlcv_stats()
            emit_and_log(
                f"OHLCV sources: supabase_hits={ohlcv_stats.get('supabase_hits', 0)} "
                f"yahoo_fetches={ohlcv_stats.get('yahoo_fetches', 0)}"
            )

        persist_meta: dict[str, Any] = {"configured": False, "persisted": False, "rows": 0}
        try:
            cfg = load_config(require_breeze=False, require_gemini=False)
            persist_meta = persist_breakout_stock_analysis(cfg, analysis_records)
        except ValueError as e:
            logger.warning("Breakout Supabase persist skipped: %s", e)
            persist_meta = {
                "configured": False,
                "persisted": False,
                "reason": "config_error",
                "message": str(e),
            }
        except Exception as e:  # pragma: no cover
            logger.warning("Breakout Supabase persist failed: %s", e)
            persist_meta = {
                "configured": True,
                "persisted": False,
                "reason": "unexpected",
                "message": str(e),
            }

        report_markdown = _build_report_markdown(all_results, scan_date)
        if write_report:
            report_path.write_text(report_markdown, encoding="utf-8")
            if emit_to_stdout:
                if all_results:
                    print("\n Consolidated Daily Breakout & Audit report saved successfully!")
                    print(f" File Link: daily_breakout_report_v2.md (file://{report_path})")
                else:
                    print(" No breakout setups found today. Empty report generated.")
                print(f" Diagnostic log: {log_path}")

        finished_at = datetime.datetime.now()
        tier_candidate_counts = {
            "Small-Cap (Nifty Smallcap 100)": 0,
            "Micro-Cap (Nifty Microcap 250)": 0,
        }
        all_results.sort(
            key=lambda r: (r.get("Composite Rank") is None, -(r.get("Composite Rank") or 0)),
        )
        for row in all_results:
            tier_candidate_counts[row["Tier"]] = tier_candidate_counts.get(row["Tier"], 0) + 1

        email_report_markdown = report_markdown if write_report else None
        emailed_ok, email_status = _send_breakout_success_email(
            scan_date=scan_date,
            tickers_scanned=scan_ticker_count,
            all_results=all_results,
            report_markdown=email_report_markdown,
        )
        emit_line(email_status)

        return {
            "ok": True,
            "run_id": run_id,
            "scan_date": scan_date_iso,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_sec": round((finished_at - started_at).total_seconds(), 2),
            "tickers_scanned": scan_ticker_count,
            "tier_ticker_counts": tier_ticker_counts,
            "candidate_count": len(all_results),
            "tier_candidate_counts": tier_candidate_counts,
            "analysis_row_count": len(analysis_records),
            "persist_meta": persist_meta,
            "candidates": [serialize_candidate(row) for row in all_results],
            "report_path": str(report_path.relative_to(_repo_root())).replace("\\", "/"),
            "log_path": str(log_path.relative_to(_repo_root())).replace("\\", "/"),
            "report_markdown": email_report_markdown,
            "email_sent": emailed_ok,
        }
    except Exception as exc:
        _send_breakout_failure_email(exc)
        raise


def main() -> None:
    try:
        run_breakout_scan()
    except Exception:
        raise


if __name__ == "__main__":
    main()
