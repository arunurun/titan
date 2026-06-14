#!/usr/bin/env python3
"""Idempotent free-NSE EOD feed ingestion + batch entrypoint.

Pulls whole-market NSE archive/JSON feeds and upserts them into the additive tables
created by sql/create_eod_feeds_tables.sql. Every feed is independent and resilient:
a failure in one feed (including a missing table) is recorded in eod_feed_ingest_log
and never crashes the batch.

Feeds: delivery | ban | futures | vix | fii_dii | corp_actions  (or `all`).

Per-date archives (delivery, futures, vix, ban, corp_actions) are fetched once per session
across a backfill window. fii_dii is latest-only: NSE's /api/fiidiiTradeReact exposes no
historical date param, so the cash flow is a single most-recent snapshot (run once per batch).

Set TITAN_NSE_INGEST_THROTTLE_SEC to add a politeness delay between feed fetches.

Usage:
  python scripts/ingest_eod_feeds.py all                       # latest session, all feeds
  python scripts/ingest_eod_feeds.py delivery --date 2026-06-12
  python scripts/ingest_eod_feeds.py futures --date 2026-06-12
  python scripts/ingest_eod_feeds.py all --start 2026-05-15 --end 2026-06-12   # backfill window
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import nse_eod
from config_loader import load_config
from supabase import create_client

FEEDS = ("delivery", "ban", "futures", "vix", "fii_dii", "corp_actions")


def _log(client, feed: str, trade_date: str, status: str, count: int, detail: str = "") -> None:
    try:
        client.table("eod_feed_ingest_log").insert(
            {"feed": feed, "trade_date": trade_date, "status": status,
             "row_count": int(count), "detail": detail[:480]}
        ).execute()
    except Exception:  # noqa: BLE001 - logging must never crash the batch
        pass


def _upsert(client, table: str, rows: list[dict[str, Any]], on_conflict: str) -> tuple[int, str]:
    if not rows:
        return 0, "empty"
    try:
        for i in range(0, len(rows), 500):
            client.table(table).upsert(rows[i:i + 500], on_conflict=on_conflict).execute()
        return len(rows), "ok"
    except Exception as exc:  # noqa: BLE001
        return 0, f"error:{type(exc).__name__}:{exc}"


def ingest_delivery(client, d: date) -> None:
    df = nse_eod.fetch_sec_bhavdata_full(d)
    if df.empty:
        _log(client, "delivery", d.isoformat(), "empty", 0, "no bhavcopy")
        print(f"  delivery {d}: empty"); return
    rows = []
    for r in df.to_dict("records"):
        rows.append({
            "trade_date": d.isoformat(), "symbol": r["symbol"], "series": r.get("series") or "EQ",
            "close_price": _f(r.get("close")), "ttl_traded_qty": _i(r.get("volume")),
            "deliv_qty": _i(r.get("deliv_qty")), "deliv_per": _f(r.get("deliv_per")),
            "turnover_lacs": _f(r.get("turnover_lacs")),
        })
    n, status = _upsert(client, "delivery_daily", rows, "trade_date,symbol,series")
    _log(client, "delivery", d.isoformat(), "ok" if n else status, n, status if not n else "")
    print(f"  delivery {d}: {n} rows ({status})")


def ingest_ban(client, d: date) -> None:
    banned, ban_date = nse_eod.fetch_fno_ban_list(d)
    if not banned:
        _log(client, "ban", d.isoformat(), "empty", 0, "no ban file / empty list")
        print(f"  ban {d}: empty"); return
    eff = (ban_date or d).isoformat()
    rows = [{"trade_date": eff, "symbol": s} for s in banned]
    n, status = _upsert(client, "fno_ban_daily", rows, "trade_date,symbol")
    _log(client, "ban", eff, "ok" if n else status, n, status if not n else "")
    print(f"  ban {eff}: {n} symbols ({status})")


def ingest_futures(client, d: date) -> None:
    df = nse_eod.fetch_fo_udiff_bhavcopy(d)
    if df.empty:
        _log(client, "futures", d.isoformat(), "empty", 0, "no fo bhavcopy"); print(f"  futures {d}: empty"); return
    cols = {c.upper(): c for c in df.columns}

    def col(*names):
        for nm in names:
            if nm in cols:
                return cols[nm]
        return None

    tp = col("FININSTRMTP", "INSTRUMENT")
    sym = col("TCKRSYMB", "SYMBOL")
    xp = col("XPRYDT", "EXPIRY_DT")
    if not (tp and sym and xp):
        _log(client, "futures", d.isoformat(), "error", 0, "unexpected schema"); print(f"  futures {d}: schema?"); return
    fut = df[df[tp].astype(str).str.upper().isin(["STF", "IDF", "FUTSTK", "FUTIDX"])].copy()
    rows = []
    for s, grp in fut.groupby(sym):
        grp = grp.copy()
        grp["_xp"] = grp[xp].map(nse_eod.parse_trade_date)
        grp = grp[grp["_xp"].notna()].sort_values("_xp")
        if grp.empty:
            continue
        front = grp.iloc[0]
        rows.append({
            "trade_date": d.isoformat(), "symbol": str(s).strip().upper(),
            "expiry_date": front["_xp"].isoformat(),
            "close_price": _f(front.get(col("CLSPRIC", "CLOSE"))),
            "settle_price": _f(front.get(col("STTLMPRIC", "SETTLE_PR"))),
            "open_interest": _i(front.get(col("OPNINTRST", "OPEN_INT"))),
            "change_in_oi": _i(front.get(col("CHNGINOPNINTRST", "CHG_IN_OI"))),
            "contracts_traded": _i(front.get(col("TTLTRADGVOL", "CONTRACTS"))),
            "underlying_close": _f(front.get(col("UNDRLYGPRIC", "UNDERLYING"))),
        })
    n, status = _upsert(client, "futures_daily", rows, "trade_date,symbol,expiry_date")
    _log(client, "futures", d.isoformat(), "ok" if n else status, n, status if not n else "")
    print(f"  futures {d}: {n} underlyings ({status})")


def ingest_vix(client, d: date) -> None:
    df = nse_eod.fetch_index_close_all(d)
    if df.empty:
        _log(client, "vix", d.isoformat(), "empty", 0, "no index file"); print(f"  vix {d}: empty"); return
    name_col = next((c for c in df.columns if "index name" in c.lower()), df.columns[0])
    vix = df[df[name_col].astype(str).str.contains("VIX", case=False, na=False)]
    if vix.empty:
        _log(client, "vix", d.isoformat(), "empty", 0, "no VIX row"); print(f"  vix {d}: no VIX row"); return
    r = vix.iloc[0].to_dict()

    def pick(*subs):
        for c in df.columns:
            lc = c.lower()
            if all(s in lc for s in subs):
                return nse_eod._num(r.get(c)) if hasattr(nse_eod, "_num") else _f(r.get(c))
        return None

    row = {
        "trade_date": d.isoformat(), "open": pick("open"), "high": pick("high"),
        "low": pick("low"), "close": pick("closing"), "prev_close": pick("prev"),
        "change_pct": pick("change", "%") or pick("change", "pct"),
    }
    if row["close"] is None:
        row["close"] = pick("close")
    n, status = _upsert(client, "india_vix_daily", [row], "trade_date")
    _log(client, "vix", d.isoformat(), "ok" if n else status, n, status if not n else "")
    print(f"  vix {d}: close={row['close']} ({status})")


def ingest_fii_dii(client, d: date) -> None:
    rows = nse_eod.fetch_fii_dii_cash()
    n, status = _upsert(client, "institutional_flow", rows, "as_of_date,segment")
    eff = rows[0]["as_of_date"] if rows else d.isoformat()
    _log(client, "fii_dii", eff, "ok" if n else status, n, status if not n else "")
    print(f"  fii_dii {eff}: {n} rows ({status})")


def ingest_corp_actions(client, d: date) -> None:
    # NSE filters corporate actions by ex-date, so fetch the single session (per-date) so a
    # window backfill accumulates multi-date history instead of only the upcoming calendar.
    rows = nse_eod.fetch_corporate_actions(from_date=d, to_date=d)
    n, status = _upsert(client, "corporate_actions_calendar", rows, "symbol,ex_date,purpose")
    _log(client, "corp_actions", d.isoformat(), "ok" if n else status, n, status if not n else "")
    print(f"  corp_actions {d}: {n} actions ({status})")


_HANDLERS: dict[str, Callable] = {
    "delivery": ingest_delivery, "ban": ingest_ban, "futures": ingest_futures,
    "vix": ingest_vix, "fii_dii": ingest_fii_dii, "corp_actions": ingest_corp_actions,
}
# Feeds with a genuine per-session NSE archive: iterate once per day in a backfill window.
# (ban -> dated fo_secban archive; corp_actions -> ex-date filtered API.)
_DATED = {"delivery", "futures", "vix", "ban", "corp_actions"}
# fii_dii is intentionally NOT here: NSE only publishes the latest cash session
# (/api/fiidiiTradeReact has no historical date param), so it is a single-shot snapshot.


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _throttle_sec() -> float:
    """Optional politeness delay between per-feed NSE fetches (env, default 0)."""
    raw = os.environ.get("TITAN_NSE_INGEST_THROTTLE_SEC", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("feed", choices=("all",) + FEEDS)
    ap.add_argument("--date", type=str, default="")
    ap.add_argument("--start", type=str, default="")
    ap.add_argument("--end", type=str, default="")
    args = ap.parse_args()

    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    if args.start and args.end:
        days = []
        d = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
    else:
        one = date.fromisoformat(args.date) if args.date else datetime.now().date()
        days = [one]

    feeds = FEEDS if args.feed == "all" else (args.feed,)
    throttle = _throttle_sec()
    print(f"Ingesting feeds={feeds} over {len(days)} session(s)")
    for d in days:
        print(f"[{d.isoformat()}]")
        for f in feeds:
            # Latest-only snapshot feeds (fii_dii) have no historical archive; run them once
            # (on the last day) to avoid redundant identical writes in a window.
            if f not in _DATED and len(days) > 1 and d != days[-1]:
                continue
            try:
                _HANDLERS[f](client, d)
            except Exception as exc:  # noqa: BLE001 - resilient batch
                _log(client, f, d.isoformat(), "error", 0, f"{type(exc).__name__}:{exc}")
                print(f"  {f} {d}: ERROR {type(exc).__name__}: {exc}")
            if throttle:
                time.sleep(throttle)
    print("DONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
