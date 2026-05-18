"""Sector priority ranking utilities (NSE market-cap enriched, Supabase persisted)."""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from postgrest.exceptions import APIError
from supabase import create_client

from breeze_client import fetch_equity_data, volume_participation_ratio
from config_loader import TitanConfig
from sector_registry import SectorInstrument

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_NSE_HOME_URL = "https://www.nseindia.com"
_NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
_YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
_MONEYCONTROL_SUGGEST_URL = (
    "https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php?query={query}&type=1&format=json"
)
_SCREENER_SEARCH_URL = "https://www.screener.in/api/company/search/?q={query}"


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _round_or_none(x: float, digits: int = 4) -> float | None:
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _bucket_from_market_cap_cr(market_cap_inr_cr: float | None) -> str:
    if market_cap_inr_cr is None:
        return "unknown"
    if market_cap_inr_cr < 5_000.0:
        return "micro"
    if market_cap_inr_cr < 20_000.0:
        return "small"
    if market_cap_inr_cr < 50_000.0:
        return "mid"
    return "large"


def _cap_bias(bucket: str) -> float:
    if bucket == "micro":
        return 8.0
    if bucket == "small":
        return 6.0
    if bucket == "mid":
        return 3.0
    if bucket == "large":
        return 1.0
    return 0.0


def _build_nse_payload_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/",
        },
    )


def _http_get_text(url: str, *, timeout_seconds: float = 20.0) -> tuple[str | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*,text/html",
            "Referer": "https://www.google.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return None, f"http_{int(getattr(e, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, "request_error"


def _fetch_nse_json(symbol: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if not sym:
        return {}
    # Warm cookie/session.
    try:
        urllib.request.urlopen(
            _build_nse_payload_request(_NSE_HOME_URL),
            timeout=timeout_seconds,
        ).read()
    except Exception:
        return {}
    try:
        with urllib.request.urlopen(
            _build_nse_payload_request(_NSE_QUOTE_URL.format(symbol=sym)),
            timeout=timeout_seconds,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_nse_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    payload = _fetch_nse_json(symbol)
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    raw = info.get("marketCap")
    v = _safe_float(raw)
    if math.isnan(v) or v <= 0.0:
        return None, "nse_quote_missing"
    # NSE marketCap is commonly rupees. Convert to INR crore.
    # 1 crore = 10,000,000 INR.
    if v > 10_000_000.0:
        return round(v / 10_000_000.0, 2), "nse_quote_rupees"
    # Defensive fallback for already-crore values.
    return round(v, 2), "nse_quote_crore"


def fetch_moneycontrol_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    query = urllib.parse.quote(symbol.strip().upper())
    raw, err = _http_get_text(_MONEYCONTROL_SUGGEST_URL.format(query=query))
    if raw is None:
        return None, f"moneycontrol_suggest_{err}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "moneycontrol_suggest_invalid_json"
    items = payload if isinstance(payload, list) else []
    link_src = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        s = str(item.get("sc_id", "")).strip().upper()
        if s == symbol.strip().upper():
            link_src = str(item.get("link_src", "")).strip()
            break
        if not link_src:
            link_src = str(item.get("link_src", "")).strip()
    if not link_src:
        return None, "moneycontrol_suggest_missing_link"
    page_raw, page_err = _http_get_text(link_src)
    if page_raw is None:
        return None, f"moneycontrol_quote_{page_err}"
    m = re.search(
        r"Mkt Cap \(Rs\. Cr\.\)\s*</td>\s*<td[^>]*>\s*([0-9,]+(?:\.[0-9]+)?)\s*</td>",
        page_raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, "moneycontrol_quote_missing_cap"
    val = _safe_float(m.group(1).replace(",", ""))
    if math.isnan(val) or val <= 0.0:
        return None, "moneycontrol_quote_invalid_cap"
    return round(val, 2), "moneycontrol_quote_rs_cr"


def fetch_screener_market_cap_inr_cr(symbol: str) -> tuple[float | None, str]:
    query = urllib.parse.quote(symbol.strip().upper())
    raw, err = _http_get_text(_SCREENER_SEARCH_URL.format(query=query))
    if raw is None:
        return None, f"screener_search_{err}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "screener_search_invalid_json"
    rows = payload if isinstance(payload, list) else []
    comp_url = ""
    for item in rows:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name", "")).strip().upper()
        if n == symbol.strip().upper():
            comp_url = str(item.get("url", "")).strip()
            break
        if not comp_url:
            comp_url = str(item.get("url", "")).strip()
    if not comp_url:
        return None, "screener_search_missing_url"
    url = f"https://www.screener.in{comp_url}" if comp_url.startswith("/") else comp_url
    page_raw, page_err = _http_get_text(url)
    if page_raw is None:
        return None, f"screener_quote_{page_err}"
    m = re.search(
        r"Market Cap</span>\s*<span class=\"number\">\s*([0-9,]+(?:\.[0-9]+)?)\s*</span>\s*Cr\.",
        page_raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None, "screener_quote_missing_cap"
    val = _safe_float(m.group(1).replace(",", ""))
    if math.isnan(val) or val <= 0.0:
        return None, "screener_quote_invalid_cap"
    return round(val, 2), "screener_quote_rs_cr"


def _yahoo_ticker(symbol: str, exchange: str) -> str:
    ex = exchange.strip().upper()
    if ex == "NSE":
        return f"{symbol}.NS"
    if ex == "BSE":
        return f"{symbol}.BO"
    return symbol


def fetch_yahoo_market_cap_inr_cr(symbol: str, exchange: str) -> tuple[float | None, str]:
    ticker = _yahoo_ticker(symbol.strip().upper(), exchange)
    url = _YAHOO_QUOTE_URL.format(symbols=urllib.parse.quote(ticker))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, f"yahoo_quote_http_{int(getattr(e, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, "yahoo_quote_error"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "yahoo_quote_invalid_json"
    qr = payload.get("quoteResponse") if isinstance(payload, dict) else {}
    result = qr.get("result") if isinstance(qr, dict) else []
    first = result[0] if isinstance(result, list) and result else {}
    if not isinstance(first, dict):
        return None, "yahoo_quote_missing"
    market_cap = _safe_float(first.get("marketCap"))
    if math.isnan(market_cap) or market_cap <= 0.0:
        return None, "yahoo_quote_missing"
    currency = str(first.get("currency", "")).strip().upper()
    if currency and currency != "INR":
        return None, f"yahoo_quote_currency_{currency.lower()}"
    return round(market_cap / 10_000_000.0, 2), "yahoo_quote_rupees"


def _load_previous_market_caps(
    cfg: TitanConfig,
    *,
    sector_key: str,
) -> dict[tuple[str, str], tuple[float, str]]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        res = (
            client.table("sector_priority_rankings")
            .select("symbol,exchange,market_cap_inr_cr,market_cap_bucket")
            .eq("sector_key", sector_key)
            .order("as_of_date", desc=True)
            .limit(1000)
            .execute()
        )
    except Exception:
        return {}
    rows = list(getattr(res, "data", None) or [])
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        ex = str(row.get("exchange", "")).strip().upper()
        cap = _safe_float(row.get("market_cap_inr_cr"))
        bucket = str(row.get("market_cap_bucket", "")).strip().lower()
        if not sym or ex not in ("NSE", "BSE"):
            continue
        if math.isnan(cap) or cap <= 0.0:
            continue
        key = (sym, ex)
        if key not in out:
            out[key] = (cap, bucket if bucket else _bucket_from_market_cap_cr(cap))
    return out


def _return_pct(series: pd.Series, periods_back: int) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= periods_back:
        return float("nan")
    prev = float(s.iloc[-(periods_back + 1)])
    last = float(s.iloc[-1])
    if prev == 0.0:
        return float("nan")
    return ((last / prev) - 1.0) * 100.0


def _score_from_features(*, bucket: str, ret_1w: float, ret_1m: float, absorption: float) -> float:
    ret_1w_term = 0.0 if math.isnan(ret_1w) else (ret_1w * 1.1)
    ret_1m_term = 0.0 if math.isnan(ret_1m) else (ret_1m * 0.45)
    absorption_term = 0.0 if math.isnan(absorption) else ((absorption - 1.0) * 8.0)
    score = _cap_bias(bucket) + ret_1w_term + ret_1m_term + absorption_term
    return round(score, 4)


def build_sector_rankings(
    cfg: TitanConfig,
    *,
    sector_key: str,
    instruments: list[SectorInstrument],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    from breeze_client import create_breeze_session

    breeze = create_breeze_session(cfg)
    as_of_date = datetime.now(IST).date().isoformat()
    prev_caps = _load_previous_market_caps(cfg, sector_key=sector_key)
    rows: list[dict[str, Any]] = []
    for inst in instruments:
        issues: list[str] = []
        try:
            df = fetch_equity_data(
                cfg,
                inst.symbol,
                inst.exchange,
                breeze=breeze,
                lookback_calendar_days=90,
                max_retries=2,
            )
        except Exception as exc:
            logger.warning("Ranking data fetch failed for %s (%s): %s", inst.symbol, inst.exchange, exc)
            df = pd.DataFrame()
            issues.append("price_history_fetch_error")
        close_col = "close" if "close" in df.columns else (df.columns[-1] if len(df.columns) > 0 else None)
        series = pd.to_numeric(df[close_col], errors="coerce") if close_col is not None else pd.Series(dtype=float)
        ret_1w = _return_pct(series, periods_back=5)
        ret_1m = _return_pct(series, periods_back=20)
        absorption = volume_participation_ratio(df) if not df.empty else float("nan")
        market_cap_cr, market_cap_source = fetch_nse_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_moneycontrol_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_screener_market_cap_inr_cr(inst.symbol)
        if market_cap_cr is None:
            market_cap_cr, market_cap_source = fetch_yahoo_market_cap_inr_cr(inst.symbol, inst.exchange)
        if market_cap_cr is None:
            prev = prev_caps.get((inst.symbol, inst.exchange))
            if prev is not None:
                market_cap_cr = round(float(prev[0]), 2)
                market_cap_source = "prior_snapshot"
        bucket = _bucket_from_market_cap_cr(market_cap_cr)
        if market_cap_cr is None:
            issues.append("market_cap_missing")
        if df.empty:
            issues.append("price_history_missing")
        if math.isnan(ret_1w):
            issues.append("return_1w_missing")
        if math.isnan(ret_1m):
            issues.append("return_1m_missing")
        if math.isnan(absorption):
            issues.append("absorption_missing")
        score = _score_from_features(
            bucket=bucket,
            ret_1w=ret_1w,
            ret_1m=ret_1m,
            absorption=absorption,
        )
        rows.append(
            {
                "sector_key": sector_key,
                "symbol": inst.symbol,
                "exchange": inst.exchange,
                "as_of_date": as_of_date,
                "market_cap_inr_cr": market_cap_cr,
                "market_cap_bucket": bucket,
                "return_1w_pct": _round_or_none(ret_1w, digits=3),
                "return_1m_pct": _round_or_none(ret_1m, digits=3),
                "absorption_ratio": _round_or_none(absorption, digits=4),
                "rank_score": score,
                "meta": {
                    "market_cap_source": market_cap_source,
                    "rows_count": int(len(df)),
                    "issues": sorted(set(issues)),
                },
            }
        )
    ranked = sorted(
        rows,
        key=lambda r: (_safe_float(r.get("rank_score")), _safe_float(r.get("return_1w_pct"))),
        reverse=True,
    )
    top_n = max(1, int(top_n))
    priority_candidates = [r for r in ranked if int((r.get("meta") or {}).get("rows_count") or 0) > 0]
    # Primary objective: small/micro-cap AI names for higher-move opportunity.
    preferred = [r for r in priority_candidates if str(r.get("market_cap_bucket")) in ("micro", "small")]
    fallback = [r for r in priority_candidates if str(r.get("market_cap_bucket")) not in ("micro", "small")]
    ordered_candidates = preferred + fallback
    priority_keys = {
        (r["symbol"], r["exchange"])
        for r in ordered_candidates[:top_n]
    }
    for i, row in enumerate(ranked, start=1):
        row["rank_in_sector"] = i
        row["is_priority"] = (row["symbol"], row["exchange"]) in priority_keys
    return ranked


def persist_sector_rankings(cfg: TitanConfig, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"persisted": False, "reason": "no_rows"}
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    try:
        sector_key = str(rows[0].get("sector_key", "")).strip().lower()
        as_of_date = str(rows[0].get("as_of_date", "")).strip()
        if sector_key and as_of_date:
            client.table("sector_priority_rankings").delete().eq("sector_key", sector_key).eq(
                "as_of_date", as_of_date
            ).execute()
        client.table("sector_priority_rankings").upsert(
            rows,
            on_conflict="sector_key,symbol,exchange,as_of_date",
        ).execute()
        return {"persisted": True, "rows": len(rows)}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return {"persisted": False, "reason": "missing_table", "message": msg}
        return {"persisted": False, "reason": "api_error", "message": msg}


def load_priority_instruments(
    cfg: TitanConfig,
    *,
    sector_key: str,
    top_n: int | None = None,
) -> list[SectorInstrument]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of = datetime.now(IST).date().isoformat()
    q = (
        client.table("sector_priority_rankings")
        .select("symbol,exchange,rank_in_sector")
        .eq("sector_key", sector_key)
        .eq("as_of_date", as_of)
        .eq("is_priority", True)
        .order("rank_in_sector")
    )
    if top_n is not None:
        q = q.limit(max(1, int(top_n)))
    try:
        res = q.execute()
    except Exception as exc:
        logger.warning("Priority load failed for sector=%s: %s", sector_key, exc)
        return []
    data = list(getattr(res, "data", None) or [])
    out: list[SectorInstrument] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        ex = str(row.get("exchange", "")).strip().upper()
        if not sym or ex not in ("NSE", "BSE"):
            continue
        out.append(SectorInstrument(symbol=sym, exchange=ex))
    return out


def persist_daily_winners(
    cfg: TitanConfig,
    *,
    sector_key: str,
    top_n: int = 10,
) -> dict[str, Any]:
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    as_of = datetime.now(IST).date().isoformat()
    q = (
        client.table("sector_priority_rankings")
        .select(
            "symbol,exchange,rank_score,market_cap_bucket,return_1w_pct,return_1m_pct,absorption_ratio,meta,rank_in_sector,is_priority"
        )
        .eq("sector_key", sector_key)
        .eq("as_of_date", as_of)
        .eq("is_priority", True)
        .order("rank_in_sector")
        .limit(max(1, int(top_n)))
    )
    try:
        res = q.execute()
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        return {"persisted": False, "reason": "ranking_read_failed", "message": msg}
    rows = list(getattr(res, "data", None) or [])
    if not rows:
        return {"persisted": False, "reason": "no_priority_rows"}

    to_upsert: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        issues = meta.get("issues") if isinstance(meta.get("issues"), list) else []
        to_upsert.append(
            {
                "sector_key": sector_key,
                "as_of_date": as_of,
                "winner_rank": i,
                "symbol": str(row.get("symbol", "")).strip().upper(),
                "exchange": str(row.get("exchange", "")).strip().upper(),
                "rank_score": _safe_float(row.get("rank_score")),
                "market_cap_bucket": str(row.get("market_cap_bucket", "unknown")).strip().lower() or "unknown",
                "score_breakdown": {
                    "return_1w_pct": row.get("return_1w_pct"),
                    "return_1m_pct": row.get("return_1m_pct"),
                    "absorption_ratio": row.get("absorption_ratio"),
                },
                "issue_flags": issues,
                "source_meta": {
                    "market_cap_source": meta.get("market_cap_source"),
                    "rows_count": meta.get("rows_count"),
                    "rank_in_sector": row.get("rank_in_sector"),
                },
            }
        )
    winners_table = client.table("sector_daily_winners")
    try:
        # Keep daily persistence idempotent even when the underlying uniqueness
        # constraint is on (sector_key, as_of_date, symbol, exchange).
        winners_table.delete().eq("sector_key", sector_key).eq("as_of_date", as_of).execute()
        winners_table.upsert(
            to_upsert,
            on_conflict="sector_key,as_of_date,winner_rank",
        ).execute()
        return {"persisted": True, "rows": len(to_upsert)}
    except APIError as e:
        payload = e.args[0] if e.args else {}
        msg = payload.get("message", str(e)) if isinstance(payload, dict) else str(e)
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        if code == "PGRST205" or "could not find the table" in msg.lower():
            return {"persisted": False, "reason": "missing_table", "message": msg}
        return {"persisted": False, "reason": "api_error", "message": msg}

