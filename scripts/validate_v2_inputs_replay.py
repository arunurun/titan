#!/usr/bin/env python3
"""Validate P0-3: signal_v2 Layers C (money-flow), C-8 (over-extension) and D
(ADX-regime / stale-flow) now fire for the 12 reviewed stocks because the risk-gate
inputs are populated in tape_extras.

BEFORE = strip cmf_20/adx_14/obv_slope_20/ema200_stretch_atr (+pctiles) from the audit
(simulates the pre-backfill state where those layers ran at zero).
AFTER  = use the backfilled tape_extras as stored.

Prints, per stock: the newly-populated data points and the count of C/C-8/D trace
terms BEFORE vs AFTER, plus the resulting label and risk_net.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from signal_v2 import evaluate_signal_v2
from signal_v2_backtest import feature_row_to_audit
from supabase import create_client

SYMBOLS = ["CANBK","ABB","INDIGO","DIXON","HINDPETRO","DIVISLAB",
           "MAHABANK","PNB","CANFINHOME","GARFIBRES","GREAVESCOT","EICHERMOT"]
STRIP_KEYS = ("cmf_20","obv_slope_20","adx_14","adx_plus_di_14","adx_minus_di_14",
              "ema200_stretch_atr","sector_pctile_ema200_stretch",
              "sector_pctile_cmf_20","sector_pctile_adx_14")


def layer_term_counts(audit: dict) -> dict:
    payload = dict(audit)
    label, risk_net, _ = evaluate_signal_v2(payload)
    trace = (payload.get("signal_reason_trace") or {}).get("terms") or []
    mods = (payload.get("signal_reason_trace") or {}).get("modifiers") or []
    c_money = sum(1 for t in trace if t.get("group") == "money_flow")
    c8_overext = sum(1 for t in trace if t.get("group") == "over_extension")
    d_adx = sum(1 for m in mods if "ADX" in m or "stale-flow" in m or "pullback" in m or "divergence" in m)
    return {"label": label, "risk_net": risk_net, "C_money_flow": c_money,
            "C8_over_extension": c8_overext, "D_modifiers": d_adx}


def main() -> int:
    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    cols = ("trade_date,sector,symbol,exchange,z_score,ema_200_distance_pct,atr_14_pct,"
            "return_1d_pct,intent_score,effective_intent_score,next_day_score,next_week_score,"
            "volume_participation_ratio,absorption_ratio,action_signal,tape_extras")
    res = (client.table("symbol_daily_features").select(cols)
           .in_("symbol", SYMBOLS).gte("trade_date", "2026-05-31").lte("trade_date", "2026-06-12")
           .order("trade_date").execute())
    by_sym: dict[str, list] = {}
    for r in (res.data or []):
        by_sym.setdefault(r["symbol"], []).append(r)

    print("=" * 118)
    print("P0-3 VALIDATION — signal_v2 risk-gate inputs now populated (BEFORE strips them, AFTER uses backfill)")
    print("=" * 118)
    hdr = (f"{'symbol':<11}{'date':>11} | {'cmf_20':>7}{'adx_14':>7}{'stretch':>8}{'obv_slope':>13} | "
           f"{'C_mf':>5}{'C8_oe':>6}{'D_mod':>6} (BEFORE)  ->  {'C_mf':>5}{'C8_oe':>6}{'D_mod':>6} (AFTER)  label")
    print(hdr); print("-" * len(hdr))
    agg_before = {"C_money_flow": 0, "C8_over_extension": 0, "D_modifiers": 0}
    agg_after = {"C_money_flow": 0, "C8_over_extension": 0, "D_modifiers": 0}
    for s in SYMBOLS:
        rows = by_sym.get(s, [])
        if not rows:
            print(f"{s:<11} (no rows)")
            continue
        r = rows[-1]  # latest in window
        audit_after = feature_row_to_audit(r) or {}
        audit_before = dict(audit_after)
        for k in STRIP_KEYS:
            audit_before.pop(k, None)
        # ensure BEFORE truly lacks stretch (feature_row_to_audit re-derives it from cols)
        audit_before["ema200_stretch_atr"] = float("nan")
        b = layer_term_counts(audit_before)
        a = layer_term_counts(audit_after)
        for kk in agg_before:
            agg_before[kk] += b[kk]; agg_after[kk] += a[kk]
        te = r.get("tape_extras") or {}
        print(f"{s:<11}{r['trade_date']:>11} | {te.get('cmf_20'):>7}{te.get('adx_14'):>7}"
              f"{(round(te.get('ema200_stretch_atr'),2) if te.get('ema200_stretch_atr') is not None else None)!s:>8}"
              f"{te.get('obv_slope_20')!s:>13} | "
              f"{b['C_money_flow']:>5}{b['C8_over_extension']:>6}{b['D_modifiers']:>6}          ->  "
              f"{a['C_money_flow']:>5}{a['C8_over_extension']:>6}{a['D_modifiers']:>6}          {a['label']}")
    print("-" * len(hdr))
    print(f"{'TOTALS':<22} | C/C-8/D fired terms  BEFORE={agg_before}  AFTER={agg_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
