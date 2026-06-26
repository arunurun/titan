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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .breakout_evidence import (
        composite_rank_score,
        compute_evidence_metrics,
        persistence_pass_min,
    )
except ImportError:
    from breakout_evidence import (
        composite_rank_score,
        compute_evidence_metrics,
        persistence_pass_min,
    )

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
RSI_MAX_HOT = 75.0
HOT_VOL_THRESHOLD = 5.0
SMA20_RECLAIM_VOL_THRESHOLD = 5.0
MICRO_CAP_VOL_CONTINUATION = 2.5
VOL_CUM_DAYS = 3

# Pre-signal validation (T-15..T-1 history before signal day T)
PRE_SIGNAL_FULL_LOOKBACK = 15
PRE_SIGNAL_CUM_RETURN_LOOKBACK = 10
PRE_SIGNAL_CUM_RETURN_MAX = 30.0
PRE_SIGNAL_VOL_SPIKE_MULT = 2.0
PRE_SIGNAL_VOL_SPIKE_DAYS_MAX = 2
PRE_SIGNAL_ADX_TRAJECTORY_LOOKBACK = 10
PRE_SIGNAL_ADX_SOFT_SHORT_LOOKBACK = 5
PRE_SIGNAL_COOLDOWN_SESSIONS = 20
PRE_SIGNAL_COOLDOWN_CONSOLIDATION_MAX = 12.0
PRE_SIGNAL_COOLDOWN_DIST_20D_HIGH_MIN = -3.0
ADX_SOFT_VOL_BONUS = 0.5
ADX_SOFT_CUM_RETURN_MAX = 20.0
POWER_GAP_CUM_RETURN_MAX = 15.0
STANDARD_ADX_TRAJECTORY_VOL_EXCEPTION = 7.0
POWER_GAP_VOL_RECOVERY_THRESHOLD = 5.5
SIGNAL_TIER_PASS = "PASS"
SIGNAL_TIER_WATCH = "WATCH"

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


def fetch_yahoo_data(ticker):
    """Fetches ~1 year of daily historical stock data from Yahoo Finance API."""
    ticker = _resolve_yahoo_ticker(ticker)
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
    """Require rising ADX on standard, adx_soft, or rsi_hot paths.

    Standard path (empty pass_paths): falling ADX allowed when vol_mult >= 7.0.
    """
    is_standard = not pass_paths
    needs_gate = is_standard or "adx_soft" in pass_paths or "rsi_hot" in pass_paths
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
) -> str:
    if signal_tier == SIGNAL_TIER_WATCH:
        parts: list[str] = ["WATCHLIST"]
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
    if power_gap:
        parts.append("circuit-risk")
    parts.append("HIGH CIRCUIT RISK")
    suffix = "Enforce tight capital sizing (1-2%). Audit GSM/ASM status."
    if pass_paths:
        suffix += f" Pass paths: {', '.join(pass_paths)}."
    return ": ".join(parts) + (f": {suffix}" if parts else suffix)


def evaluate_bars_as_of(
    df: dict[str, Any],
    as_of_idx: int,
    tier_name: str,
    *,
    last_pass_idx: int | None = None,
    bhav_turnover_lacs: float | None = None,
) -> dict[str, Any]:
    """Point-in-time breakout filter evaluation using bars[0..as_of_idx] inclusive.

    Alternate pass paths (tracked in pass_paths / risk_flags):
    - power_gap: pct_change 12–20% with circuit-risk flag
    - vol_continuation_cum3d: 3-session cumulative vol >= tier threshold
    - vol_continuation_prior_spike: micro-cap 2.5x after prior spike session
    - sma20_reclaim: price > SMA20 with vol > 5x despite below SMA50
    - rsi_hot: RSI 70–75 when vol > 5x
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
    sma50_last = sma_50[-1]
    vol_20_avg_last = vol_20_avg[-1]
    latest_volume = volume[-1]

    sma_200 = calculate_sma(close, 200) if len(close) > 200 else [0.0] * len(close)
    sma_200_last = sma_200[-1] if sma_200 else None
    poc_window = close[-30:] if len(close) >= 30 else close
    vol_window = volume[-30:] if len(volume) >= 30 else volume
    poc = get_volume_profile(poc_window, vol_window)

    sl_price = min(sma_50[-1], poc) * 0.98
    risk = latest_price - sl_price
    target_price = latest_price + (2.0 * risk)
    target_gain = ((target_price - latest_price) / latest_price) * 100

    pass_paths: list[str] = []
    power_gap = False

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

    if fail_reason is None:
        rsi_ok = RSI_MIN <= rsi_val <= RSI_MAX_NORMAL
        if not rsi_ok and RSI_MAX_NORMAL < rsi_val <= RSI_MAX_HOT and vol_mult > HOT_VOL_THRESHOLD:
            rsi_ok = True
            pass_paths.append("rsi_hot")
        if not rsi_ok:
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

    evidence = compute_evidence_metrics(
        df, as_of_idx, tier_name, vol_20_avg, bhav_turnover_lacs=bhav_turnover_lacs,
    )
    metrics["evidence"] = evidence
    metrics["liquidity_quality"] = evidence.get("liquidity_quality")
    metrics["persistence_score"] = evidence.get("persistence_score")
    metrics["breakout_stage"] = evidence.get("breakout_stage")
    metrics["base_score"] = evidence.get("base_score")
    metrics["median_turnover_inr"] = evidence.get("median_turnover_inr")

    if fail_reason is None and not evidence.get("liquidity_gate_pass", True):
        pre_filter_fail = evidence.get("liquidity_gate_fail") or "pre_filter_liquidity"
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

    if v7_watch_reason:
        metrics["v7_watch_reason"] = v7_watch_reason
        metrics["risk_flags"] = _format_risk_flags(
            power_gap=power_gap,
            pass_paths=pass_paths,
            signal_tier=signal_tier,
        )

    metrics["composite_rank"] = composite_rank_score(metrics)

    metrics["pass_paths"] = pass_paths
    metrics["signal_tier"] = signal_tier
    if not v7_watch_reason:
        metrics["risk_flags"] = _format_risk_flags(
            power_gap=power_gap,
            pass_paths=pass_paths,
            signal_tier=signal_tier,
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

def evaluate_and_audit_stock(ticker, tier_name, emit=None):
    filt = FILTERS[tier_name]
    vol_thresh = filt["vol_mult"]
    df, fetch_err = fetch_yahoo_data(ticker)
    if not df or len(df["close"]) < 50:
        if fetch_err:
            fail_reason = f"insufficient_data ({fetch_err})"
        elif df:
            fail_reason = f"insufficient_data (only {len(df['close'])} bars)"
        else:
            fail_reason = "insufficient_data"
        _emit_stock_diagnostic(emit, ticker, tier_name, fail_reason=fail_reason)
        return None

    eval_result = evaluate_bars_as_of(df, len(df["close"]) - 1, tier_name)
    latest_price = eval_result["latest_price"]
    pct_change = eval_result["pct_change"]
    vol_mult = eval_result["vol_mult"]
    rsi_val = eval_result["rsi_val"]
    adx_val = eval_result["adx_val"]
    sma50_last = eval_result["sma50_last"]
    sl_price = eval_result["sl_price"]
    target_price = eval_result["target_price"]
    target_gain = eval_result["target_gain"]
    risk_flags = eval_result["risk_flags"]

    fail_reason = eval_result.get("fail_reason")
    signal_tier = eval_result.get("signal_tier")
    if fail_reason:
        _emit_stock_diagnostic(
            emit, ticker, tier_name, latest_price, pct_change,
            vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
            status="FAIL", fail_reason=fail_reason,
        )
        return None

    status = signal_tier or "PASS"
    _emit_stock_diagnostic(
        emit, ticker, tier_name, latest_price, pct_change,
        vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
        status=status, fail_reason="",
    )

    sma_200_last = eval_result.get("sma_200_last") or 0.0
    poc = eval_result["poc"]

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

    return {
        "Ticker": ticker.replace(".NS", ""),
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
        "Pass Paths": ", ".join(pass_paths) if pass_paths else "",
        "Watch Reason": watch_reason or "",
        "Payload": payload
    }


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


def _sort_report_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PASS first, then WATCH, each group by composite rank descending."""

    def _key(row: dict[str, Any]) -> tuple[int, float]:
        tier_rank = 0 if row.get("Signal Tier") == SIGNAL_TIER_PASS else 1
        composite = row.get("Composite Rank")
        rank_val = float(composite) if isinstance(composite, (int, float)) else 0.0
        return (tier_rank, -rank_val)

    return sorted(rows, key=_key)


def serialize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a scanner row for JSON API responses."""
    change_raw = str(row.get("Change", "")).lstrip("▲▼● ").lstrip("+").rstrip("%").strip()
    vol_raw = str(row.get("Volume Mult", "")).rstrip("x")
    gain_raw = str(row.get("Est. Gain", "")).rstrip("%")
    return {
        "ticker": row["Ticker"],
        "tier": row["Tier"],
        "price": row["Price"],
        "change_pct": float(change_raw) if change_raw else None,
        "change_display": row["Change"],
        "volume_mult": float(vol_raw) if vol_raw else None,
        "volume_mult_display": row["Volume Mult"],
        "rsi": row["RSI"],
        "adx": row["ADX"],
        "entry_range": row["Entry Range"],
        "est_stop_loss": row["Est. Stop-Loss"],
        "est_target": row["Est. Target (1:2)"],
        "est_gain_pct": float(gain_raw) if gain_raw else None,
        "est_gain_display": row["Est. Gain"],
        "risk_flags": row["Risk Flags"],
        "signal_tier": row.get("Signal Tier"),
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
        tables_md += f"\n### {tier_name} Breakouts\n"
        if not rows:
            tables_md += "No stocks met the criteria in this tier today.\n"
            continue

        sorted_rows = _sort_report_candidates(rows)
        tables_md += (
            "| Ticker | Signal Tier | Breakout Stage | Persistence | LQ | Base | Rank | "
            "Price | Change | Vol | RSI | ADX | Entry | Stop | Target | Gain | Pass Paths | Risk Flags |\n"
        )
        tables_md += (
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | "
            ":--- | :--- | :--- | :--- | :--- | :--- |\n"
        )

        for r in sorted_rows:
            tables_md += (
                f"| {r['Ticker']} | {_format_signal_tier_badge(r.get('Signal Tier'))} | "
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
        f"({len(pass_rows)} PASS, {len(watch_rows)} WATCH)</p>"
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
        f"{summary}{pass_section}{watch_section}{top_section}"
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
    candidate_count = len(all_results)
    has_signal_tier = any("Signal Tier" in row for row in all_results)

    lines = [
        f"Titan breakout scan — {scan_date.isoformat()}",
        "",
        f"Tickers scanned: {tickers_scanned}",
    ]
    if has_signal_tier:
        lines.append(
            f"Candidates: {candidate_count} total ({pass_count} PASS, {watch_count} WATCH)"
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
) -> bool:
    from email_notify import send_success_post_email

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
        logger.info("Breakout scan success email sent.")
    else:
        logger.info("Breakout scan success email skipped (SMTP not configured).")
    return emailed_ok


def _send_breakout_failure_email(exc: BaseException) -> bool:
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
    tier_ticker_counts: dict[str, int] = {}
    scan_ticker_count = 0

    try:
        with log_path.open("w", encoding="utf-8") as log_file:

            def emit_and_log(line: str) -> None:
                emit_line(line)
                log_file.write(line + "\n")
                log_file.flush()

            emit_and_log(f"=== Breakout scanner run {started_at.isoformat()} ===")
            warm_yahoo_session()
            emit_and_log("Yahoo session warm-up complete.")

            for tier_key, url in INDEX_URLS.items():
                tier_label = FILTERS[tier_key]["type"]
                if emit_to_stdout:
                    print(f"Downloading constituent list for {tier_label} ...")
                tickers = download_nse_tickers(url)
                tier_ticker_counts[tier_label] = len(tickers)
                if not tickers:
                    if emit_to_stdout:
                        print(f" Warning: No tickers found for {tier_key}. Skipping.")
                    continue

                total = len(tickers)
                if emit_to_stdout:
                    print(f" Found {total} tickers. Scanning & Auditing technicals...")

                for ticker in tickers:
                    res = evaluate_and_audit_stock(ticker, tier_key, emit=emit_and_log)
                    if res:
                        all_results.append(res)
                    scan_ticker_count += 1
                    if scan_ticker_count % 50 == 0:
                        msg = f"Chunk cool-down: {scan_ticker_count} tickers scanned, sleeping 120s..."
                        emit_and_log(msg)
                        time.sleep(120)
                if emit_to_stdout:
                    print("\n Scan & Audit complete for this tier.\n")

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
        emailed_ok = _send_breakout_success_email(
            scan_date=scan_date,
            tickers_scanned=scan_ticker_count,
            all_results=all_results,
            report_markdown=email_report_markdown,
        )

        return {
            "ok": True,
            "scan_date": scan_date.isoformat(),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_sec": round((finished_at - started_at).total_seconds(), 2),
            "tickers_scanned": scan_ticker_count,
            "tier_ticker_counts": tier_ticker_counts,
            "candidate_count": len(all_results),
            "tier_candidate_counts": tier_candidate_counts,
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
