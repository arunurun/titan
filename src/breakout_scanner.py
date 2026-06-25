"""Small & micro-cap breakout scanner for Titan control UI and CLI."""

from __future__ import annotations

import csv
import datetime
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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


def download_nse_tickers(url):
    """Downloads and parses an official NSE index CSV file to extract ticker symbols."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        tickers = []
        with urllib.request.urlopen(req) as response:
            content = response.read().decode('utf-8').splitlines()
            reader = csv.reader(content)
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
                        symbol = symbol.replace("&", "%26")
                        symbol = symbol.replace("%26", "")
                        tickers.append(symbol + ".NS")
    except Exception as e:
        print(f"Warning: Failed to download tickers from {url}: {e}")
    return tickers

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

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    latest_price = close[-1]
    prev_price = close[-2]
    pct_change = ((latest_price - prev_price) / prev_price) * 100

    sma_50 = calculate_sma(close, 50)
    vol_20_avg = calculate_sma(volume, 20)
    vol_mult = volume[-1] / (vol_20_avg[-1] if vol_20_avg[-1] > 0 else 1.0)
    rsi = calculate_rsi(close, 14)
    adx_arr, _, _ = calculate_adx(high, low, close, period=14)
    rsi_val = rsi[-1]
    adx_val = adx_arr[-1]
    sma50_last = sma_50[-1]

    fail_reason = None
    if latest_price < filt["min_price"]:
        fail_reason = "min_price"
    elif not (3.0 <= pct_change <= 12.0):
        fail_reason = "pct_change"
    elif latest_price < sma50_last:
        fail_reason = "SMA50"
    elif vol_mult < vol_thresh:
        fail_reason = "vol"
    elif not (50.0 <= rsi_val <= 70.0):
        fail_reason = "RSI"
    elif adx_val < 25.0:
        fail_reason = "ADX"

    if fail_reason:
        _emit_stock_diagnostic(
            emit, ticker, tier_name, latest_price, pct_change,
            vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
            status="FAIL", fail_reason=fail_reason,
        )
        return None

    sma_200 = calculate_sma(close, 200) if len(close) > 200 else [0.0] * len(close)
    poc = get_volume_profile(close[-30:], volume[-30:])

    sl_price = min(sma_50[-1], poc) * 0.98
    risk = latest_price - sl_price
    target_price = latest_price + (2.0 * risk)
    target_gain = ((target_price - latest_price) / latest_price) * 100

    if target_gain < 8.0:
        _emit_stock_diagnostic(
            emit, ticker, tier_name, latest_price, pct_change,
            vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
            status="FAIL", fail_reason="target_gain",
        )
        return None

    _emit_stock_diagnostic(
        emit, ticker, tier_name, latest_price, pct_change,
        vol_mult, vol_thresh, rsi_val, adx_val, sma50_last,
        status="PASS", fail_reason="",
    )

    payload = f"""**TECHNICAL GROUNDING DATA: {ticker.replace(".NS", "")}**
Data Retrieval Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (IST)
Latest Market Price: {round(latest_price, 2)} ({'+' if pct_change >= 0 else ''}{round(pct_change, 2)}% today)
Relative Volume (20-day Average): {round(vol_mult, 2)}x
Relative Strength Index (RSI 14): {round(rsi[-1], 2)}
Average Directional Index (ADX 14): {round(adx_arr[-1], 2)}

Trend Indicators:
* 50-period SMA: {round(sma_50[-1], 2)} (Price is {'ABOVE' if latest_price > sma_50[-1] else 'BELOW'} 50 SMA)
* 200-period SMA: {round(sma_200[-1], 2)} (Price is {'ABOVE' if latest_price > sma_200[-1] else 'BELOW'} 200 SMA)

Volume Profile (30-day Visible Range):
* Point of Control (POC): {round(poc, 2)}
"""

    return {
        "Ticker": ticker.replace(".NS", ""),
        "Tier": filt["type"],
        "Price": round(latest_price, 2),
        "Change": f"+{round(pct_change, 2)}%",
        "Volume Mult": f"{round(vol_mult, 2)}x",
        "RSI": round(rsi[-1], 2),
        "ADX": round(adx_arr[-1], 2),
        "Entry Range": f"{round(latest_price * 0.985, 2)} - {round(latest_price * 1.01, 2)}",
        "Est. Stop-Loss": round(sl_price, 2),
        "Est. Target (1:2)": round(target_price, 2),
        "Est. Gain": f"{round(target_gain, 2)}%",
        "Risk Flags": "HIGH CIRCUIT RISK: Enforce tight capital sizing (1-2%). Audit GSM/ASM status.",
        "Payload": payload
    }


def serialize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a scanner row for JSON API responses."""
    change_raw = str(row.get("Change", "")).lstrip("+").rstrip("%")
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

        tables_md += (
            "| Ticker | Price | Change | Volume Mult | RSI | ADX | Est. Entry | Est. Stop-Loss | "
            "Est. Target | Est. Gain | Risk Flags |\n"
        )
        tables_md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"

        for r in rows:
            tables_md += (
                f"| {r['Ticker']} | {r['Price']} | {r['Change']} | {r['Volume Mult']} | "
                f"{r['RSI']} | {r['ADX']} | {r['Entry Range']} | {r['Est. Stop-Loss']} | "
                f"{r['Est. Target (1:2)']} | {r['Est. Gain']} | {r['Risk Flags']} |\n"
            )
            detailed_payloads_md += f"### {r['Ticker']} Technical Grounding Data\n"
            detailed_payloads_md += "```markdown\n"
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

    with log_path.open("w", encoding="utf-8") as log_file:

        def emit_and_log(line: str) -> None:
            emit_line(line)
            log_file.write(line + "\n")
            log_file.flush()

        emit_and_log(f"=== Breakout scanner run {started_at.isoformat()} ===")
        warm_yahoo_session()
        emit_and_log("Yahoo session warm-up complete.")
        scan_ticker_count = 0

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
    for row in all_results:
        tier_candidate_counts[row["Tier"]] = tier_candidate_counts.get(row["Tier"], 0) + 1

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
        "report_markdown": report_markdown if write_report else None,
    }


def main() -> None:
    run_breakout_scan()


if __name__ == "__main__":
    main()
