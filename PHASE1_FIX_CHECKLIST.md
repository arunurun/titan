# Titan "Buy-Then-Decline" Fix — Checklist

Branch: `14thJune` · Last updated: 2026-06-14

## Root cause (data-confirmed against last 10 sessions)
The weekly "buy" you act on is the weekend **winners shortlist** (`sector_daily_winners`), whose
`rank_score` (`sector_priority._score_from_features`) is **pure trailing momentum with no overbought
brake and no regime gate** — so it picks already-extended names that mean-revert. A secondary
artifact: broad up-days (e.g. 06-10) inflate scores via the **same-day move**, which then fades.

Verification result: 6 of 9 buy-rated names declined — 4 overextension (ABB, GREAVESCOT, DIXON,
EICHERMOT) + 2 hostile-regime PSU banks (CANBK, PNB). HPCL & DIVISLAB were **never bought** (Titan
correctly trimmed them). GARFIBRES (+7.8%) & MAHABANK (+2.7%) were extended but rose — so fixes must
not blindly kill momentum.

---

## DONE (implemented + backtested on `14thJune`, uncommitted for review)

- [x] **A — Overbought / overextension penalty**
  - Smooth penalty added to winner `rank_score` in `sector_priority.py` (stretch channel + run
    channel + volume amp + run-gate), recorded in `score_breakdown`. Env knobs `TITAN_OVEREXT_*`.
  - Mirrored in `signal_v2` Layer C: lowered C-8 stretch deadband 4.0→3.0 ATR + new symmetric
    upside-z term. Env knobs `TITAN_SIGV2_C_UPSIDE_Z_*`.
  - Calibration result: ABB / GREAVESCOT / EICHERMOT demoted; GARFIBRES + MAHABANK protected
    (run-gate added to kill a MAHABANK false-positive). DIXON & CANBK/PNB are honest misses (not
    statistically extended by stored features — need fix B / delivery% instead).

- [x] **C — Contemporaneous-score de-bias**
  - Soft same-day-pop dampener in `sector_audit.py` using `session_move_vs_prev_close_pct`; shaves
    only the above-neutral score slice + positive z (never softens downside). Env knobs `TITAN_CONTEMP_*`.

- [x] **Tests + 12-stock replay**
  - Touched-module tests: 149 passed; 9 failures are pre-existing `BREEZE_API_KEY` env errors
    (fail identically on baseline; no tests modified).
  - `scripts/replay_12_stocks_ac.py` built + run → BEFORE/AFTER table produced (see chat / re-run
    `python scripts/replay_12_stocks_ac.py`).

### Result scorecard
- 3 of 4 named decliners down-ranked (ABB, GREAVESCOT, EICHERMOT); + HINDPETRO penalized.
- Both risers preserved (GARFIBRES, MAHABANK, also INDIGO/CANFINHOME) — no riser wrongly killed.
- Misses: DIXON (stretch only 2.55), CANBK/PNB (regime-driven, not overbought) → covered by deferred
  fix **B (regime gate)** + delivery%, not by an overbought penalty.

---

## DEFERRED (planned, not being done in this batch)

### Rest of Phase 1
- [ ] **B — Sector-regime gate** (`sector_daily_rollup.breadth_above_ema200_pct < ~40%` / falling
      `avg_effective_intent_score` → damp/skip new buys). Would have caught CANBK/PNB. *Deferred per
      your "implement A, C" instruction.*
- [ ] **D — signal_v2 BUY-gate overextension ceilings** (z-score / ema-distance / 20d-high proximity)
      for literal `buy`/`accumulate` labels (e.g. ABB 06-01 buy at stretch 4.17).
- [ ] **Delivery % ingestion + churn gate** (NSE EOD `sec_bhavdata_full`). *Needs data first — not in
      DB yet, so can't be validated against history.* Trailing-delivery gate + post-close reconcile.
- [ ] **F&O ban-list + circuit/price-band veto.** *Needs data first — not in DB.*

### Phase 2 — positioning & honest validation
- [ ] Activate dormant FII/DII `institutional_flow` block (cash + FII derivatives stats).
- [ ] Futures OI / basis / rollover for F&O names → flag short-covering pops vs long buildup.
- [ ] Fix backtest + reconcile to use **forward** returns (+1/+5 sessions); add per-buy win-rate and
      post-signal drawdown; de-contaminate the shortlist hit-rate (currently same-day = circular).
- [ ] Forward-return / hit-rate feedback loop (persist per-signal outcomes).
- [ ] Next-open/gap entry guard + activate the designed-but-inert signal hysteresis.

### Phase 3 — event / distress filters
- [ ] Results & corporate-actions calendar + valuation-vs-history percentile.
- [ ] Promoter-pledge spike + SLB borrow-trend distress flags.
- [ ] Harden data-freshness (stale/expired Breeze session → hard buy-withhold + alert); verify
      symbol→instrument mapping for the 12 names.

---

## Decisions locked
- `eithermont` resolved to **EICHERMOT (Eicher Motors)** (ERIS had zero stored rows).
- Window for analysis: **last 10 trading sessions** (≈ 2026-05-31 .. 06-12).
- No code is committed; all work stays on `14thJune` for your review.
- Data sources beyond Breeze (delivery %, FII/DII, futures OI, VIX/regime, ban/circuit) are all
  available **free** from official NSE EOD reports (same scrape pattern Titan already uses).
