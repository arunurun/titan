#!/usr/bin/env python3
"""KEY VALIDATION (read-only): fix-ON vs fix-OFF, scored against realized FORWARD returns.

Pulls stored symbol_daily_features over the dense window and, for a broad universe
(12 named stocks + defence/ai/telecom sector members + a random control sample),
evaluates two scoring lenses with the fixes ON (default env) vs OFF:

  LENS 1  WINNER RANK (Fix A, sector_priority._overextension_penalty)
          - rank sector members by rank_score; compare top-N selections.
  LENS 2  BUY-RATING (Fix C contemporaneous de-bias + Fix A signal_v2 C-8 mirror)
          - recompute scores via sector_audit._refresh_symbol_scoring_outputs and
            label via signal_v2.evaluate_signal_v2; compare buy-rated names.

FORWARD returns are computed from each symbol's date-ordered return_1d_pct series:
the next available session(s) AFTER the signal date (never same-day). A calendar-gap
guard (<= MAX_GAP_DAYS) prevents bridging the 05-17 -> 05-31 data gap.

NO writes to Supabase. Env knobs are toggled in-process (all gates read os.environ live).
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from config_loader import load_config
from signal_v2 import evaluate_signal_v2
from signal_v2_backtest import feature_row_to_audit, audit_has_signal_inputs
import sector_priority as sp
from sector_audit import _refresh_symbol_scoring_outputs
from supabase import create_client

NAMED = ["CANBK", "ABB", "INDIGO", "DIXON", "HINDPETRO", "DIVISLAB",
         "MAHABANK", "PNB", "CANFINHOME", "GARFIBRES", "GREAVESCOT", "EICHERMOT"]
TARGET_SECTORS = ["defence", "ai", "telecom"]
SIGNAL_START = "2026-05-31"   # dense block only (avoids the 05-17 -> 05-31 gap)
FETCH_START = "2026-05-15"
END = "2026-06-12"
MAX_GAP_DAYS = 4
BUY_LABELS = {"buy", "accumulate"}
N_CONTROL = 60
TOP_N = 3
SEED = 20260614


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _tape(row) -> dict:
    t = row.get("tape_extras")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except Exception:
            t = {}
    return t if isinstance(t, dict) else {}


def _pdate(s: str) -> date:
    y, m, d = (int(x) for x in str(s)[:10].split("-"))
    return date(y, m, d)


def fetch_all():
    cfg = load_config(require_breeze=False, require_gemini=False)
    cl = create_client(cfg.supabase_url, cfg.supabase_key)
    cols = ("trade_date,symbol,sector,exchange,z_score,ema_200_distance_pct,atr_14_pct,"
            "return_1d_pct,intent_score,effective_intent_score,next_day_score,next_week_score,"
            "volume_participation_ratio,absorption_ratio,action_signal,tape_extras")
    rows, off, step = [], 0, 1000
    while True:
        res = (cl.table("symbol_daily_features").select(cols)
               .gte("trade_date", FETCH_START).lte("trade_date", END)
               .order("trade_date").range(off, off + step - 1).execute())
        d = res.data or []
        rows.extend(d)
        if len(d) < step:
            break
        off += step
    return rows


def build_forward(rows):
    """symbol -> {date_str -> (fwd_1d_pct, fwd_5d_pct, n_fwd)}."""
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    fwd = {}
    for sym, rs in by_sym.items():
        rs = sorted(rs, key=lambda r: r["trade_date"])
        ds = [_pdate(r["trade_date"]) for r in rs]
        rets = [_f(r.get("return_1d_pct")) for r in rs]
        out = {}
        for i in range(len(rs)):
            # walk forward across contiguous sessions (gap-guarded)
            cum, one, k = 1.0, float("nan"), 0
            j = i + 1
            while j < len(rs) and k < 5:
                if (ds[j] - ds[j - 1]).days > MAX_GAP_DAYS:
                    break
                rj = rets[j]
                if math.isnan(rj):
                    break
                if k == 0:
                    one = rj
                cum *= (1.0 + rj / 100.0)
                k += 1
                j += 1
            fwd_5 = (cum - 1.0) * 100.0 if k > 0 else float("nan")
            out[rs[i]["trade_date"]] = (one, fwd_5, k)
        fwd[sym] = out
    return fwd


def winner_score(row, *, penalty_on: bool):
    """rank_score with uniform cap-bias (0): isolates the Fix A penalty's effect."""
    t = _tape(row)
    ret5 = _f(t.get("return_5d_pct"))
    ret20 = _f(t.get("return_20d_pct"))
    absorp = _f(row.get("absorption_ratio"))
    stretch = _f(t.get("ema200_stretch_atr"))
    ema = _f(row.get("ema_200_distance_pct"))
    base = ((0.0 if math.isnan(ret5) else ret5 * 1.1)
            + (0.0 if math.isnan(ret20) else ret20 * 0.45)
            + (0.0 if math.isnan(absorp) else (absorp - 1.0) * 8.0))
    if math.isnan(ret5) and math.isnan(ret20) and math.isnan(absorp):
        return float("nan"), 0.0
    pen = 0.0
    if penalty_on:
        pen = sp._overextension_penalty(ret_1w=ret5, ret_1m=ret20, absorption=absorp,
                                        stretch=stretch, ema_dist=ema)["penalty"]
    return round(base - pen, 4), pen


def buy_label(row, *, contemp_on: bool, overext_mirror_on: bool):
    """Full pipeline label under the given fix configuration."""
    audit = feature_row_to_audit(row)
    if audit is None or not audit_has_signal_inputs(audit):
        return None
    # configure env for this evaluation
    os.environ["TITAN_CONTEMP_DAMPENER_ENABLED"] = "1" if contemp_on else "0"
    if overext_mirror_on:
        # Fix A mirror ON = defaults (C-8 deadband 3.0, upside-z cap 1.5)
        for k in ("TITAN_SIGV2_C_STRETCH_DEADBAND_ATR", "TITAN_SIGV2_C_UPSIDE_Z_CAP"):
            os.environ.pop(k, None)
    else:
        # Pre-Fix-A emulation: restore old C-8 deadband 4.0 and remove the upside-z
        # term (cap=0 -> full_points=0 -> term never fires). NB: do NOT raise the
        # deadband above the ramp, which would invert _ramp and MAX the penalty.
        os.environ["TITAN_SIGV2_C_STRETCH_DEADBAND_ATR"] = "4.0"
        os.environ["TITAN_SIGV2_C_UPSIDE_Z_CAP"] = "0.0"
    _refresh_symbol_scoring_outputs(audit)  # applies Fix C, recomputes next_week
    label, risk_net, _ = evaluate_signal_v2(audit)
    return str(label).strip().lower()


def stat(vals):
    vals = [v for v in vals if not math.isnan(v)]
    if not vals:
        return (0, float("nan"), float("nan"))
    n = len(vals)
    decl = sum(1 for v in vals if v < 0.0)
    return (n, sum(vals) / n, 100.0 * decl / n)


def main():
    random.seed(SEED)
    rows = fetch_all()
    fwd = build_forward(rows)

    # universe membership
    sec_members = defaultdict(set)
    all_syms_by_sector = defaultdict(set)
    for r in rows:
        sec = str(r.get("sector") or "").lower()
        all_syms_by_sector[sec].add(r["symbol"])
        if sec in TARGET_SECTORS:
            sec_members[sec].add(r["symbol"])
    target_syms = set(NAMED)
    for s in TARGET_SECTORS:
        target_syms |= sec_members[s]
    other_syms = sorted({r["symbol"] for r in rows
                         if str(r.get("sector") or "").lower() not in TARGET_SECTORS}
                        - set(NAMED))
    control = set(random.sample(other_syms, min(N_CONTROL, len(other_syms))))

    print("=" * 110)
    print("KEY VALIDATION: fix-ON vs fix-OFF scored against realized FORWARD returns (next session onward)")
    print(f"Fetch {FETCH_START}..{END}; signals from {SIGNAL_START} (dense block); forward up to 5 sessions, gap<= {MAX_GAP_DAYS}d")
    print("=" * 110)
    print(f"Universe: 12 named + defence({len(sec_members['defence'])}) + ai({len(sec_members['ai'])}) "
          f"+ telecom({len(sec_members['telecom'])}) + control({len(control)} random other-sector)")

    # ---------------- LENS 1: WINNER RANK (Fix A) ----------------
    print("\n" + "#" * 110)
    print("# LENS 1 - WINNER RANK (Fix A overextension penalty): top-%d per sector/date, ON vs OFF" % TOP_N)
    print("#" * 110)
    by_sec_date = defaultdict(list)
    for r in rows:
        if r["trade_date"] < SIGNAL_START:
            continue
        sec = str(r.get("sector") or "").lower()
        if sec not in TARGET_SECTORS:
            continue
        by_sec_date[(sec, r["trade_date"])].append(r)

    sel_on_fwd1, sel_on_fwd5 = [], []
    sel_off_fwd1, sel_off_fwd5 = [], []
    demoted_cases = []   # names dropped by ON that OFF kept
    promoted_cases = []  # names added by ON that OFF dropped
    for (sec, d), members in sorted(by_sec_date.items()):
        scored_on, scored_off = [], []
        for r in members:
            son, pen = winner_score(r, penalty_on=True)
            soff, _ = winner_score(r, penalty_on=False)
            if math.isnan(son):
                continue
            f1, f5, k = fwd.get(r["symbol"], {}).get(d, (float("nan"), float("nan"), 0))
            if k == 0:
                continue
            scored_on.append((son, r["symbol"], f1, f5, pen))
            scored_off.append((soff, r["symbol"], f1, f5, pen))
        if len(scored_on) < TOP_N + 1:
            continue
        top_on = sorted(scored_on, reverse=True)[:TOP_N]
        top_off = sorted(scored_off, reverse=True)[:TOP_N]
        on_syms = {x[1] for x in top_on}
        off_syms = {x[1] for x in top_off}
        for _, sym, f1, f5, pen in top_on:
            sel_on_fwd1.append(f1); sel_on_fwd5.append(f5)
        for _, sym, f1, f5, pen in top_off:
            sel_off_fwd1.append(f1); sel_off_fwd5.append(f5)
        for _, sym, f1, f5, pen in top_off:
            if sym not in on_syms:  # OFF picked it, ON demoted it out of top-N
                demoted_cases.append((sec, d, sym, pen, f5))
        for _, sym, f1, f5, pen in top_on:
            if sym not in off_syms:
                promoted_cases.append((sec, d, sym, pen, f5))

    n1, a1, dr1 = stat(sel_off_fwd5)
    n2, a2, dr2 = stat(sel_on_fwd5)
    print(f"\nTop-{TOP_N} selections pooled across defence/ai/telecom x {len(by_sec_date)} sector-dates")
    print(f"  OFF (no penalty):  picks={n1}  avg fwd-5d={a1:+.2f}%  buy-then-decline={dr1:.1f}%")
    print(f"  ON  (penalty):     picks={n2}  avg fwd-5d={a2:+.2f}%  buy-then-decline={dr2:.1f}%")
    print(f"  DELTA (ON-OFF):    avg fwd-5d={a2-a1:+.2f} pp   decline-rate={dr2-dr1:+.1f} pp")
    of1n, of1a, of1d = stat(sel_off_fwd1); on1n, on1a, on1d = stat(sel_on_fwd1)
    print(f"  (fwd-1d: OFF avg={of1a:+.2f}% decl={of1d:.1f}% | ON avg={on1a:+.2f}% decl={on1d:.1f}%)")

    print(f"\n  Names ON demoted OUT of top-{TOP_N} that OFF had kept ({len(demoted_cases)}): "
          f"avg fwd-5d of those names = {stat([c[4] for c in demoted_cases])[1]:+.2f}%")
    for sec, d, sym, pen, f5 in sorted(demoted_cases, key=lambda c: c[4])[:12]:
        tag = "GOOD(it fell)" if (not math.isnan(f5) and f5 < 0) else ("BAD(it rose)" if not math.isnan(f5) else "n/a")
        print(f"     {d} {sec:<9}{sym:<12} penalty={pen:5.2f} fwd5d={f5:+7.2f}%  {tag}")
    print(f"\n  Names ON promoted INTO top-{TOP_N} that OFF dropped ({len(promoted_cases)}): "
          f"avg fwd-5d = {stat([c[4] for c in promoted_cases])[1]:+.2f}%")
    for sec, d, sym, pen, f5 in sorted(promoted_cases, key=lambda c: c[4])[:12]:
        tag = "GOOD(it rose)" if (not math.isnan(f5) and f5 >= 0) else ("BAD(it fell)" if not math.isnan(f5) else "n/a")
        print(f"     {d} {sec:<9}{sym:<12} promoted          fwd5d={f5:+7.2f}%  {tag}")

    # ---------------- LENS 2: BUY-RATING (Fix C + Fix A mirror) ----------------
    print("\n" + "#" * 110)
    print("# LENS 2 - BUY-RATING (Fix C de-bias + Fix A signal_v2 C-8 mirror): buy/accumulate names, ON vs OFF")
    print("#" * 110)

    def run_universe(name, syms):
        on_f1, on_f5, off_f1, off_f5 = [], [], [], []
        flips = []  # (sym,date,off_label,on_label,f5)
        n_signals = 0
        for r in rows:
            if r["trade_date"] < SIGNAL_START or r["symbol"] not in syms:
                continue
            f1, f5, k = fwd.get(r["symbol"], {}).get(r["trade_date"], (float("nan"), float("nan"), 0))
            if k == 0:
                continue
            lab_on = buy_label(r, contemp_on=True, overext_mirror_on=True)
            lab_off = buy_label(r, contemp_on=False, overext_mirror_on=False)
            if lab_on is None or lab_off is None:
                continue
            n_signals += 1
            if lab_off in BUY_LABELS:
                off_f1.append(f1); off_f5.append(f5)
            if lab_on in BUY_LABELS:
                on_f1.append(f1); on_f5.append(f5)
            if (lab_off in BUY_LABELS) != (lab_on in BUY_LABELS):
                flips.append((r["symbol"], r["trade_date"], lab_off, lab_on, f5))
        no, ao, do = stat(off_f5)
        nn, an, dn = stat(on_f5)
        print(f"\n[{name}]  evaluable signals={n_signals}")
        print(f"  OFF: buy-rated={no:3d}  avg fwd-5d={ao:+.2f}%  buy-then-decline={do:.1f}%")
        print(f"  ON : buy-rated={nn:3d}  avg fwd-5d={an:+.2f}%  buy-then-decline={dn:.1f}%")
        if no and nn:
            print(f"  DELTA: count={nn-no:+d}  avg fwd-5d={an-ao:+.2f} pp  decline-rate={dn-do:+.1f} pp")
        # buys removed by ON (off-only) and their realized fwd
        removed = [x for x in flips if x[2] in BUY_LABELS and x[3] not in BUY_LABELS]
        added = [x for x in flips if x[2] not in BUY_LABELS and x[3] in BUY_LABELS]
        if removed:
            rs = stat([x[4] for x in removed])
            print(f"  ON removed {len(removed)} OFF-buys: their avg fwd-5d={rs[1]:+.2f}% decline={rs[2]:.1f}% "
                  f"(good if these fell)")
        if added:
            ad = stat([x[4] for x in added])
            print(f"  ON added {len(added)} new buys: their avg fwd-5d={ad[1]:+.2f}% decline={ad[2]:.1f}%")
        return removed, added

    run_universe("12 NAMED", set(NAMED))
    run_universe("DEFENCE", sec_members["defence"])
    run_universe("AI", sec_members["ai"])
    run_universe("TELECOM", sec_members["telecom"])
    run_universe("CONTROL (random other)", control)
    run_universe("ALL TARGET (named+def+ai+telecom)", target_syms)

    # restore env to defaults (ON)
    os.environ["TITAN_CONTEMP_DAMPENER_ENABLED"] = "1"
    for k in ("TITAN_SIGV2_C_STRETCH_DEADBAND_ATR", "TITAN_SIGV2_C_UPSIDE_Z_CAP"):
        os.environ.pop(k, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
