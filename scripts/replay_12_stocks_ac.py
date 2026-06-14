#!/usr/bin/env python3
"""Replay harness for Fix A (overextension penalty) + Fix C (contemporaneous de-bias).

Standalone tool (not part of core behavior). Pulls the stored last-~10-sessions rows
for the 12 reviewed stocks from Supabase and shows BEFORE (stored) vs AFTER (recomputed
under Fix A + Fix C) for the weekend WINNER rank_score and the per-session
effective_intent_score / next_week_score / z_score / action_signal, alongside the
realized forward move (cumulative from stored return_1d_pct).

Usage:
  python scripts/replay_12_stocks_ac.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from action_signals import derive_action_signal, normalize_action_signal
from config_loader import load_config
from sector_audit import _apply_contemporaneous_dampener, _contemporaneous_discount_factor
import sector_priority as sp
from supabase import create_client

SYMBOLS = [
    "CANBK", "ABB", "INDIGO", "DIXON", "HINDPETRO", "DIVISLAB",
    "MAHABANK", "PNB", "CANFINHOME", "GARFIBRES", "GREAVESCOT", "EICHERMOT",
]

START = "2026-05-31"
END = "2026-06-12"


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _tape(row: dict) -> dict:
    t = row.get("tape_extras")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except Exception:
            t = {}
    return t if isinstance(t, dict) else {}


def fetch() -> tuple[dict, dict]:
    cfg = load_config(require_breeze=False, require_gemini=False)
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    cols = (
        "trade_date,sector,symbol,exchange,intent_score,effective_intent_score,"
        "action_signal,z_score,volume_participation_ratio,absorption_ratio,"
        "next_day_score,next_week_score,return_1d_pct,ema_200_distance_pct,atr_14_pct,"
        "tape_extras"
    )
    res = (
        client.table("symbol_daily_features")
        .select(cols)
        .in_("symbol", SYMBOLS)
        .gte("trade_date", START)
        .lte("trade_date", END)
        .order("trade_date")
        .execute()
    )
    feats: dict[str, list] = {}
    for r in list(getattr(res, "data", None) or []):
        feats.setdefault(r["symbol"], []).append(r)

    res2 = (
        client.table("sector_daily_winners")
        .select("as_of_date,sector_key,symbol,winner_rank,rank_score,score_breakdown")
        .in_("symbol", SYMBOLS)
        .gte("as_of_date", "2026-05-25")
        .lte("as_of_date", "2026-05-31")
        .order("as_of_date")
        .execute()
    )
    winners: dict[str, dict] = {}
    for r in list(getattr(res2, "data", None) or []):
        # keep the most recent weekend pick per symbol
        winners[r["symbol"]] = r
    return feats, winners


def realized_forward_move(rows: list) -> float:
    cum = 1.0
    for r in rows:
        v = _f(r.get("return_1d_pct"))
        if not math.isnan(v):
            cum *= (1.0 + v / 100.0)
    return (cum - 1.0) * 100.0


def fixA_rankscore(symbol: str, feats: dict, winners: dict) -> dict:
    """Reconstruct OLD (no penalty) and NEW (with penalty) winner rank_score."""
    rows = feats.get(symbol, [])
    first = rows[0] if rows else {}
    first_tape = _tape(first)
    # stretch / ema at the earliest stored session (pick-time proxy)
    ema = _f(first.get("ema_200_distance_pct"))
    atr = _f(first.get("atr_14_pct"))
    stretch = ema / atr if (not math.isnan(ema) and not math.isnan(atr) and atr != 0.0) else float("nan")

    win = winners.get(symbol)
    if win and isinstance(win.get("score_breakdown"), dict):
        bd = win["score_breakdown"]
        r1w = _f(bd.get("return_1w_pct"))
        r1m = _f(bd.get("return_1m_pct"))
        absorp = _f(bd.get("absorption_ratio"))
        stored_rank = _f(win.get("rank_score"))
        terms = (0.0 if math.isnan(r1w) else r1w * 1.1) \
            + (0.0 if math.isnan(r1m) else r1m * 0.45) \
            + (0.0 if math.isnan(absorp) else (absorp - 1.0) * 8.0)
        cap_bias = stored_rank - terms  # implied market-cap bias
        old_score = stored_rank
        src = f"winner@{win.get('as_of_date')} rank#{win.get('winner_rank')}"
        winner_rank = win.get("winner_rank")
    else:
        # not in winners table -> reconstruct from first feature row (cap_bias unknown -> 0)
        r1w = _f(first_tape.get("return_5d_pct"))
        r1m = _f(first_tape.get("return_20d_pct"))
        absorp = _f(first.get("absorption_ratio"))
        cap_bias = 0.0
        terms = (0.0 if math.isnan(r1w) else r1w * 1.1) \
            + (0.0 if math.isnan(r1m) else r1m * 0.45) \
            + (0.0 if math.isnan(absorp) else (absorp - 1.0) * 8.0)
        old_score = round(cap_bias + terms, 4)
        stored_rank = float("nan")
        src = "reconstructed (not in winners table; r1w~r5d, r1m~r20d)"
        winner_rank = None

    pen = sp._overextension_penalty(
        ret_1w=r1w, ret_1m=r1m, absorption=absorp, stretch=stretch, ema_dist=ema
    )
    new_score = round(old_score - pen["penalty"], 4)
    return {
        "r1w": r1w, "r1m": r1m, "absorp": absorp, "stretch": stretch, "ema": ema,
        "cap_bias": round(cap_bias, 3),
        "old_score": round(old_score, 3),
        "penalty": pen["penalty"],
        "components": pen["components"],
        "new_score": new_score,
        "winner_rank": winner_rank,
        "source": src,
    }


def fixC_peak_session(symbol: str, feats: dict) -> dict:
    """Find the biggest same-day-pop session and show Fix C de-bias there."""
    rows = feats.get(symbol, [])
    best = None
    best_move = -1e9
    for r in rows:
        tape = _tape(r)
        sess = _f(tape.get("session_move_vs_prev_close_pct"))
        ret1d = _f(r.get("return_1d_pct"))
        move = sess if not math.isnan(sess) else ret1d
        if not math.isnan(move) and move > best_move:
            best_move = move
            best = r
    if best is None:
        return {}
    tape = _tape(best)
    # Build a minimal audit from the stored row.
    audit = {
        "symbol": symbol,
        "effective_intent_score": _f(best.get("effective_intent_score")),
        "intent_score": _f(best.get("intent_score")),
        "z_score": _f(best.get("z_score")),
        "return_1d_pct": _f(best.get("return_1d_pct")),
        "return_5d_pct": _f(tape.get("return_5d_pct")),
        "return_10d_pct": _f(tape.get("return_10d_pct")),
        "rel_return_5d_vs_nifty_pct": _f(tape.get("rel_return_5d_vs_nifty_pct")),
        "rel_return_20d_vs_nifty_pct": _f(tape.get("rel_return_20d_vs_nifty_pct")),
        "ema_200_distance_pct": _f(best.get("ema_200_distance_pct")),
        "atr_14_pct": _f(best.get("atr_14_pct")),
        "session_move_vs_prev_close_pct": _f(tape.get("session_move_vs_prev_close_pct")),
    }
    old_eff = audit["effective_intent_score"]
    old_z = audit["z_score"]
    old_nw = _f(best.get("next_week_score"))
    old_action = normalize_action_signal(best.get("action_signal") or "")

    move, frac = _contemporaneous_discount_factor(audit)

    # NEW path: apply Fix C dampener, recompute predictive scores + action signal.
    new_audit = dict(audit)
    from sector_audit import _refresh_symbol_scoring_outputs
    _refresh_symbol_scoring_outputs(new_audit)
    new_eff = _f(new_audit.get("effective_intent_score"))
    new_z = _f(new_audit.get("z_score"))
    new_nw = _f(new_audit.get("next_week_score"))
    new_action = normalize_action_signal(new_audit.get("action_signal") or "")

    return {
        "date": best.get("trade_date"),
        "same_day_move": round(move, 3) if not math.isnan(move) else None,
        "discount_frac": round(frac, 4),
        "old_eff": round(old_eff, 2) if not math.isnan(old_eff) else None,
        "new_eff": round(new_eff, 2) if not math.isnan(new_eff) else None,
        "old_z": round(old_z, 3) if not math.isnan(old_z) else None,
        "new_z": round(new_z, 3) if not math.isnan(new_z) else None,
        "old_nw": round(old_nw, 2) if not math.isnan(old_nw) else None,
        "new_nw": round(new_nw, 2) if not math.isnan(new_nw) else None,
        "old_action": old_action,
        "new_action": new_action,
    }


def main() -> int:
    feats, winners = fetch()

    print("=" * 100)
    print("REPLAY: Fix A (overextension penalty) + Fix C (contemporaneous de-bias) on the 12 stocks")
    print(f"Window: {START}..{END}   (winner picks from weekend 2026-05-29/30)")
    print("=" * 100)

    fwd = {s: realized_forward_move(feats.get(s, [])) for s in SYMBOLS}

    # ---- FIX A: winner rank_score BEFORE vs AFTER ----
    print("\n### FIX A — WINNER rank_score (BEFORE vs AFTER)\n")
    hdr = (
        f"{'symbol':<11}{'r1w%':>7}{'r1m%':>7}{'absrp':>6}{'strch':>7}"
        f"{'OLD':>9}{'penalty':>9}{'NEW':>9}{'fwd%':>8}  verdict"
    )
    print(hdr)
    print("-" * len(hdr))
    fixA = {}
    for s in SYMBOLS:
        a = fixA_rankscore(s, feats, winners)
        fixA[s] = a
        f = fwd[s]
        demoted = a["penalty"] >= 2.0
        rose = f >= 0.0
        if rose and a["penalty"] < 1.0:
            verdict = "OK (riser kept)"
        elif (not rose) and demoted:
            verdict = "GOOD (faller demoted)"
        elif (not rose) and not demoted:
            verdict = "miss (faller not demoted)"
        elif rose and demoted:
            verdict = "WARN (riser penalized)"
        else:
            verdict = "-"
        strch = "n/a" if math.isnan(a["stretch"]) else f"{a['stretch']:.2f}"
        print(
            f"{s:<11}{a['r1w']:>7.2f}{a['r1m']:>7.2f}{a['absorp']:>6.2f}{strch:>7}"
            f"{a['old_score']:>9.2f}{a['penalty']:>9.2f}{a['new_score']:>9.2f}{f:>8.2f}  {verdict}"
        )

    # ---- FIX C: peak contemporaneous session BEFORE vs AFTER ----
    print("\n### FIX C — peak same-day-pop session (BEFORE vs AFTER)\n")
    hdr2 = (
        f"{'symbol':<11}{'date':>12}{'move%':>7}{'disc':>6}"
        f"{'effOLD':>8}{'effNEW':>8}{'zOLD':>7}{'zNEW':>7}{'nwOLD':>7}{'nwNEW':>7}  act(old->new)"
    )
    print(hdr2)
    print("-" * len(hdr2))
    fixC = {}
    for s in SYMBOLS:
        c = fixC_peak_session(s, feats)
        fixC[s] = c
        if not c:
            print(f"{s:<11} (no rows)")
            continue
        print(
            f"{s:<11}{str(c['date']):>12}{(c['same_day_move'] or 0):>7.2f}{c['discount_frac']:>6.2f}"
            f"{(c['old_eff'] or 0):>8.2f}{(c['new_eff'] or 0):>8.2f}"
            f"{(c['old_z'] or 0):>7.2f}{(c['new_z'] or 0):>7.2f}"
            f"{(c['old_nw'] or 0):>7.2f}{(c['new_nw'] or 0):>7.2f}  {c['old_action']}->{c['new_action']}"
        )

    # ---- SCORECARD ----
    print("\n### SCORECARD\n")
    risers = [s for s in SYMBOLS if fwd[s] >= 0.0]
    fallers = [s for s in SYMBOLS if fwd[s] < 0.0]
    print(f"Realized forward move (cum return_1d {START}..{END}):")
    for s in sorted(SYMBOLS, key=lambda x: fwd[x]):
        tag = "FELL" if fwd[s] < 0 else "rose"
        print(f"   {s:<11} {fwd[s]:>7.2f}%   {tag}   FixA penalty={fixA[s]['penalty']:.2f}")

    demoted_fallers = [s for s in fallers if fixA[s]["penalty"] >= 2.0]
    print(f"\nFallers ({len(fallers)}): {fallers}")
    print(f"  -> down-ranked by Fix A (penalty>=2.0): {demoted_fallers} ({len(demoted_fallers)}/{len(fallers)})")
    target = ["ABB", "GREAVESCOT", "DIXON", "EICHERMOT"]
    tgt_pen = {s: fixA[s]["penalty"] for s in target}
    print(f"\nNamed overextended-and-fell targets penalties: {tgt_pen}")
    print(f"  GARFIBRES penalty={fixA['GARFIBRES']['penalty']:.2f} (fwd {fwd['GARFIBRES']:.2f}%) -- must stay ~0")
    print(f"  MAHABANK  penalty={fixA['MAHABANK']['penalty']:.2f} (fwd {fwd['MAHABANK']:.2f}%) -- must stay ~0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
