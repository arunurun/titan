# Titan Final Validation Summary

Branch: `14thJune` · Date: 2026-06-14 · Mode: read-only Supabase (no Breeze, no writes except this doc)

## Scope

Ran the forward-return harness, Fix A/C replay, gate-stack simulation, and EOD join probe over
`2026-05-15..2026-06-12`. All scripts exited 0.

| Script | Purpose |
| --- | --- |
| `scripts/forward_return_eval.py` | Cohort fwd+1/+5 win-rate, avg return, drawdown |
| `scripts/validate_fix_forward_returns.py` | Fix A/C ON vs OFF vs realized forward returns |
| `scripts/replay_12_stocks_ac.py` | 12-stock BEFORE/AFTER rank_score + contemporaneous de-bias |
| `temp/val_combined.py` | Full gate stack enforced (regime + v2-risk + delivery + ban + futures) |
| `temp/val_step1.py` / `val_step3a.py` / `val_step3b.py` / `val_step4.py` | Per-gate acceptance |
| `temp/diag_verify_feeds.py` / `_verify_eod_joins.py` | EOD row counts + symbol/date joins |

Raw outputs: `temp/_val_*.txt`

---

## Headline before/after numbers

### 1. Stored-signal forward returns (no gate simulation)

Harness: `forward_return_eval.py`, horizons +1 and +5 sessions **after** signal date.

| Cohort | Obs | Fwd+1 win% | Fwd+1 avg | Fwd+5 win% | Fwd+5 avg | Avg maxDD@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Twelve preset** | 130 | 41.5% | −0.16% | **48.6%** | **+0.34%** | −2.56% |
| **defence + ai + telecom** | 560 | 40.4% | −0.31% | **37.7%** | **−0.54%** | −4.42% |

These are the **baseline** (what stored `symbol_daily_features` rows actually did next). They do not
apply Fix A/C or any gate — they measure the universe as persisted.

### 2. Fix A/C ON vs OFF (recomputed scores, forward-scored)

Harness: `validate_fix_forward_returns.py`, dense block `2026-05-31..2026-06-12`.

| Lens | OFF (pre-fix) | ON (Fix A/C) | Delta |
| --- | --- | --- | --- |
| **Winner top-3** (def/ai/tel, 87 picks) | avg fwd-5d −1.04%, decline 60.9% | avg fwd-5d −0.96%, decline 59.8% | **+0.08 pp** avg, −1.1 pp decline |
| **Buy-rated** (all target, 64→56 buys) | avg fwd-5d +0.94%, decline 48.4% | avg fwd-5d +1.24%, decline 48.2% | **+0.31 pp** avg, −8 buys |
| **Buy-rated removed by ON** (8 names) | — | their avg fwd-5d **−1.21%** | good if these fell |

Fix A demotions that helped: MTARTECH (−10.8% fwd5), MIDHANI (−1.5%), STLTECH (−2.4%). One false
demotion: IDEA (+0.21%). Fix C had no effect on the 12 named buy-ratings (0 flips).

### 3. Full gate stack enforced (env toggles per Worker C)

Harness: `val_combined.py` — `TITAN_REGIME_GATE_MODE=skip`, `TITAN_V2_RISK_GATE_MODE=skip`,
`TITAN_DELIVERY_GATE_MODE=damp`, `TITAN_BAN_GATE_MODE=skip`, `TITAN_FUTURES_GATE_MODE=damp`.
Signal proxy date: last feature ≤ `2026-06-01`.

| Cohort | fix-OFF (no gates) | fix-ON (enforced) |
| --- | --- | --- |
| **Named-12** (n=12) | decline 67%, avg fwd+5 +0.01% | **published 2**, decline 50%, avg +0.06% |
| **Defence** (n=19) | decline 63%, avg +0.16% | **0 published** (all withheld) |
| **AI** (n=12) | decline 33%, avg +1.88% | **1 published** (GENESYS), decline 0%, avg +4.64% |
| **Telecom** (n=15) | decline 33%, avg +2.98% | **0 published** (all withheld) |

Gate stack blocks most fallers but also blocks risers (CANBK +3.83%, GREAVESCOT +5.81%, PARAS +20.6%).
**Not ready for production enforce** without calibration.

### 4. Per-fix acceptance spot checks

| Fix | Evidence |
| --- | --- |
| **P0-2 absorption** | 30 high-absorption down-day sessions: legacy bonus >5 pts zeroed (BDL +62.8→0, HAL +32.9→0) |
| **P1-3 regime** | Defence hostile @ 2026-06-03: intent 78→40, breadth 100→80; AI ok (intent rising) |
| **P1-6 v2-risk** | 31 names would-withhold; bottom fwd-5 includes BDL, MTARTECH, COCHINSHIP |
| **Shadow default** | `val_modes.py`: shadow → 0 withheld, 0 multiplier changes on defence cohort |

### 5. 12-stock replay (Fix A + Fix C)

`replay_12_stocks_ac.py`: 8/12 fell over the window. Fix A penalized 4/8 fallers (ABB, HINDPETRO,
GREAVESCOT, EICHERMOT). Misses: CANBK, DIXON, DIVISLAB, PNB (not statistically extended in stored
features). Fix C: 1 label flip (PNB trim→hold on peak pop day); no buy-rating changes on named 12.

---

## EOD pipeline verification

Row counts (`diag_verify_feeds.py`, window 2026-05-15..06-12):

| Table | Total rows | Distinct dates | Notes |
| --- | ---: | ---: | --- |
| `delivery_daily` | 55,862 | 21 | ~2,650–2,670 symbols/day |
| `futures_daily` | 4,304 | 20 | ~214–216 contracts/day; missing 2026-05-28 |
| `india_vix_daily` | 20 | 20 | 1 row/day |
| `fno_ban_daily` | 31 | 17 | 1–2 banned symbols/day |
| `institutional_flow` | 1 | 1 | **only 2026-06-12** (cash segment) |
| `corporate_actions_calendar` | 147 | 19 ex-dates | sparse by design |

Join probe (`_verify_eod_joins.py`, trade_date=2026-06-12):

- `delivery_daily` symbol match: **315/318 (99.1%)** of feature symbols
- `futures_daily` F&O subset: **139/139 (100%)**
- Date-key equality: confirmed (`CANBK` string match)
- Twelve stocks: all in features + delivery; 8/12 in futures (non-F&O names expected absent)

**Caveat:** Defence sector shows only **1 member** (ZENTEC) in `symbol_daily_features` @ 2026-06-12 —
sector-tag mapping (P1-4) is still corrupting rollup/member reads.

---

## Honest caveats

1. **Winners shortlist is stale** — `sector_daily_winners.as_of_date` = `2026-05-30`. None of Fix A/C,
   P0-2, or shadow gates are reflected in the live buy list until P0-1 Breeze re-run.
2. **All new gates ship shadow-first** — production scores unchanged; enforcement is simulation-only.
3. **Forward returns are reconstructed** from stored `return_1d_pct` (no raw close); gap between
   2026-05-17 and 2026-05-31 excluded in fix-ON/OFF harness (`MAX_GAP_DAYS=4`).
4. **Gate over-blocking** — enforced stack withholds 10/10 telecom risers and 7/8 named-12 names;
   v2-risk gate alone withholds CANBK/PNB/MAHABANK despite positive fwd+5 in the proxy window.
5. **Thin DATA dependencies** — `institutional_flow` history is 1 day; `session_move_vs_prev_close_pct`
   still sparse (P0-2 uses `return_1d_pct` fallback); sector rollups unreliable until P1-4.
6. **No live Breeze** — sector analysis, winners refresh, and intraday regime proxy not exercised.

---

## Live-run blockers

| Blocker | Severity | Next step |
| --- | --- | --- |
| Stale `sector_daily_winners` (2026-05-30) | **P0** | P0-1: Breeze re-run + persist |
| Shadow-only gates | **P0** | Measurement sign-off → flip env to damp/skip per gate |
| Sector-tag mismatch (defence 1 member) | **P1** | P1-4 DATA mapping fix |
| FII/DII history (1 row) | **P2** | Backfill `institutional_flow` window |
| Over-blocking on enforce preview | **P1** | Tune gate thresholds before enforce |
| Phase 1 live-streaming enforce | **pending** | `TITAN_LIVE_DATA_PLAN.md` P3-a |
| Promoter-pledge + SLB flags | **pending** | Phase 3 DATA |

---

## Artifacts

- Checklist updated: `TITAN_DEEPSCAN_FIX_CHECKLIST.md` (FINAL VALIDATION section)
- This summary: `VALIDATION_SUMMARY.md`
- Raw logs: `temp/_val_fwd_twelve.txt`, `_val_fwd_sectors.txt`, `_val_fix_fwd.txt`, `_val_replay12.txt`,
  `_val_combined.txt`, `_val_eod_counts.txt`, `_val_eod_joins.txt`, `_val_step*.txt`

Branch `14thJune` — **uncommitted** (checklist + summary only).
