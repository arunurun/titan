# Titan Deep-Scan Fix Checklist (Consolidated)

Branch: `14thJune` · Created: 2026-06-14 · Owner of this doc: Agent B (planning only)

This is a **planning deliverable**. No code, SQL, or Supabase data is changed by this file.
It consolidates and de-duplicates the deep-scan findings against
[`PHASE1_FIX_CHECKLIST.md`](PHASE1_FIX_CHECKLIST.md), verifies each against the live code on
`14thJune`, and adds precise `file:line` anchors, effort, dependencies, blocked-status, and a
crisp acceptance check.

Legend: **Effort** S (<½ day) / M (½–2 days) / L (>2 days). **Blocked**: `Breeze` = needs a live
Breeze session; `DATA` = owned by the data-pipeline agent (feature write-time / Supabase); `none`
= pure code change here. Priorities: **P0** = must fix before trusting any buy · **P1** = distorts
evaluation · **P2** = robustness.

---

## ROLLOUT POLICY — shadow-mode first (decided 2026-06-14)

**Every new buy-suppressing gate ships in shadow mode first, then is enforced only after the
forward-return harness confirms it helps.** This applies to the sector-regime gate and all
subsequent gates (Fix-A refinement, signal_v2 ceilings, delivery%/churn, ban-list/circuit,
short-covering, P0-2/P1-6).

Rollout sequence per gate:
1. **Shadow:** the gate computes and **logs** its would-be decision ("would block / damp / down-rank
   this buy") but **does not change** any published signal, label, or rank. Persist the shadow
   decision alongside the signal so it can be evaluated.
2. **Measure:** grade shadow decisions on **forward +1/+5-session returns + post-signal drawdown**
   (the Step-0 measurement harness, deep-scan P1-1) — i.e. would enforcing the gate have improved
   win-rate / cut drawdown on the affected cohort, without throwing away genuine winners?
3. **Enforce:** flip the gate on (behind an env flag, NaN-safe fallback) **only** once the data
   shows net-positive. Default starting enforcement strength is **damp / half-size**, tightening
   toward hard-skip only for clearly hostile cases once proven.

Rationale: lowest risk of over-blocking (false skips), and it makes the measurement harness a hard
prerequisite (Step 0) for every behavioural change. Cost: a measurement cycle of delay before the
benefit is realised.

---

## ALREADY DONE — do NOT redo (verified present on `14thJune`)

These were implemented (uncommitted) on `14thJune`; every item below is written to **not** overlap
with them.

- [x] **Fix A — Overextension penalty** in winner `rank_score`.
  Code verified: `_overextension_penalty()` `src/sector_priority.py:2028-2084`, wired into
  `_score_from_features()` `src/sector_priority.py:2127-2134`; `TITAN_OVEREXT_*` env knobs at
  `:2005-2017`. Mirror in `signal_v2` Layer C (C-8 deadband 4.0→3.0 + symmetric upside-z)
  `src/signal_v2.py:303-313, 343-365`.
- [x] **Fix C — Contemporaneous same-day-pop de-bias** in `src/sector_audit.py` using
  `session_move_vs_prev_close_pct` (`TITAN_CONTEMP_*` knobs). Shaves only above-neutral score slice
  + positive z; never softens downside.
- [x] Touched-module tests (149 pass; 9 pre-existing `BREEZE_API_KEY` failures) +
  `scripts/replay_12_stocks_ac.py`.
- [x] **P0-2 — Cap + sign-gate absorption term** (`_absorption_term()` `src/sector_priority.py:2170-2202`,
  wired into `_score_from_features` `:2218-2219`; `TITAN_ABS_TERM_*` env knobs). Shadow-first default
  (`TITAN_ABS_TERM_MODE=shadow`); enforce via env toggle. Validated 2026-06-14: BDL/HAL/BEML down-day
  bonuses zeroed; climax up-days capped at 12 pts (`temp/val_step3a.py`).
- [x] **P1-3 — Sector-regime gate (Fix B)** (`_regime_gate_decision` / `_fetch_sector_regime`
  `src/sector_priority.py:2267+`; `TITAN_REGIME_GATE_MODE` shadow/damp/skip). Validated 2026-06-14:
  Defence 05-31→06-03 fires on falling intent (breadth still 80–100%); banks_psu fires on breadth +
  intent (`temp/val_step1.py`).
- [x] **P1-6 — v2 risk-label gate** (`_v2_risk_gate` `src/sector_priority.py:2471+`;
  `TITAN_V2_RISK_GATE_MODE`). Shadow-first; withholds trim/exit-risk when enforced. Validated
  2026-06-14: BDL/MTARTECH/COCHINSHIP withheld; gate stack in `temp/val_combined.py`.
- [x] **Forward-return harness** — `scripts/forward_return_eval.py` (P1-1 core) +
  `scripts/validate_fix_forward_returns.py` + `scripts/replay_12_stocks_ac.py`; Worker C helpers in
  `temp/valharness.py`, `temp/val_combined.py`.
- [x] **EOD feeds ingested** — six tables (`delivery_daily`, `futures_daily`, `india_vix_daily`,
  `fno_ban_daily`, `institutional_flow`, `corporate_actions_calendar`); join probe
  `temp/_verify_eod_joins.py`: delivery 99.1% symbol match @ 2026-06-12; F&O subset 100%; date-key
  equality confirmed.
- [x] **Shadow gates shipped** — regime, v2-risk, delivery/churn, ban, futures OI gates all default
  `shadow` (no published-score change); enforcement preview via env toggles documented in
  `temp/val_modes.py`, `temp/val_combined.py`.

---

## P0 — Must fix before trusting any buy

### P0-1 · Refresh stale pre-fix winners shortlist
- **Symptom:** `sector_daily_winners` / `sector_priority_rankings` are stuck at `as_of_date
  2026-05-30` with `overextension_penalty=None` — i.e. the shortlist you act on predates Fix A/C, so
  none of the new brakes are reflected in the live buy list.
- **Action:** Re-run ranking + winners persistence so rows carry the new `score_breakdown`
  (`overextension_penalty`, `overextension_components`) and, once P0-2 lands, the capped/sign-gated
  absorption. No logic change required here — it is a **re-run** of the existing path.
- **Anchors:** producer `build_sector_rankings()` `src/sector_priority.py:2138+`;
  persistence `persist_daily_winners()` `src/sector_priority.py:2440-2519` (writes
  `score_breakdown` at `:2484-2493`). Stale-fallback that masks the gap:
  `src/sector_priority.py:2411-2437` (silently serves `latest` as_of when today is empty).
- **Effort:** S (re-run) · **Blocked:** `Breeze` + `DATA` (needs live session; persistence owned
  with data agent) — mark dependency.
- **Depends on:** P0-2 (so the refreshed rows already carry the absorption fix), P0-3 (so risk-gate
  inputs are present for the v2 cross-check in P1-6).
- **Done when:** newest `sector_daily_winners.as_of_date` = current trading day AND every row’s
  `score_breakdown.overextension_penalty` is non-null AND absorption contribution reflects the P0-2
  cap/sign-gate.

### P0-2 · Cap AND sign-gate the absorption term in `rank_score`  ← **DONE** (shadow-first)
- **Symptom:** The additive absorption bonus is **uncapped and sign-blind**:
  `absorption_term = 0.0 if isnan(absorption) else ((absorption - 1.0) * 8.0)`. A high-volume **down**
  day (distribution / climax sell-off) produces a huge **positive** score. Evidence: **BDL** crowned
  #1 Defence off an 8.85× volume **−6% DOWN** day → absorption = **+62.8 pts = 122% of total score**,
  while `signal_v2` flagged exit-risk; **HAL 104%**, **BEML 114%**; **NETWEB** #1 AI off a 9.25×
  climax **up**-day.
- **Action (code, here):** In `_score_from_features`:
  1. **Cap** the absorption contribution (e.g. `TITAN_ABS_TERM_CAP`, default ~12–15 pts) so it can
     never dominate trailing momentum.
  2. **Sign-gate** on same-session direction: zero (or invert toward a penalty) the bonus when the
     session closed **down** (use `session_move_vs_prev_close_pct` / `return_1d_pct` — same field
     family Fix C already consumes). Absorption on a down day is distribution, not accumulation.
  3. Add `TITAN_ABS_*` env knobs (cap, down-day multiplier/floor) mirroring the Fix-A knob pattern at
     `:2005-2017`, and record the gated value into `score_breakdown` for explainability.
- **Anchors:** `src/sector_priority.py:2125` (the offending line), function
  `_score_from_features` `:2114-2135`; thread session-direction in alongside `stretch`/`ema_dist`
  the same way Fix A threads its inputs (`:2114-2122`, `:2128-2134`). Breakdown sink:
  `persist_daily_winners` `:2484-2493`.
- **Effort:** M · **Blocked:** `none` for the logic; the down-day input needs
  `session_move_vs_prev_close_pct` (see P1-5) at ranking compute time — fall back to `return_1d_pct`
  if the session field is absent so this is **not** hard-blocked.
- **Depends on:** none to start; P1-5 improves the sign-gate input.
- **Done when:** on a replay, BDL/HAL/BEML absorption contribution ≤ cap AND is ≤0 on their down
  sessions; none of them rank #1; NETWEB up-day climax is capped (not eliminated); a unit test
  asserts `absorption_term` is bounded and non-positive when session move < 0.

### P0-3 · Persist + consume v2 risk-gate inputs (code-consumer side)
- **Symptom:** Risk-gate features (`cmf_20`, `obv_slope_20`, `adx_14`, `adx_plus_di_14`,
  `ema200_stretch_atr`, `sector_pctile_ema200_stretch`) are missing/sparse in stored features, so
  `signal_v2` Layer C and the backtest can’t reproduce live labels. (Write-time persistence is the
  **DATA** agent’s job; the **code-consumer** gap is here.)
- **Action (code, here):** In `feature_row_to_audit()` derive `ema200_stretch_atr` when absent from
  the already-stored `ema_200_distance_pct` and `atr_14_pct` (i.e. `dist_pct / atr_14_pct`, the same
  ratio `_stretch_inputs_from_df` computes at `src/sector_priority.py:2104-2111`). Ensure the other
  six keys are read through when present so `layer_c` consumes them. Confirm `signal_v2_backtest`
  reproducibility once the DATA side backfills.
- **Anchors:** consumer `layer_c` reads at `src/signal_v2.py:295-298`; audit rebuild
  `feature_row_to_audit` `src/signal_v2_backtest.py:171-204` (note it pulls `ema_200_distance_pct`
  and `atr_14_pct` at `:182-195` but never derives `ema200_stretch_atr`); the key list at
  `src/signal_v2_backtest.py:59`.
- **Effort:** S (derivation) + M (validation) · **Blocked:** `DATA` for the upstream backfill; the
  derivation itself is `none`.
- **Depends on:** DATA backfill of the six columns (parallel).
- **Done when:** `feature_row_to_audit` returns non-NaN `ema200_stretch_atr` whenever
  `ema_200_distance_pct`/`atr_14_pct` exist, and a backtest replay reproduces live `signal_v2`
  labels for a sampled set within tolerance.

---

## P1 — Distorts evaluation

### P1-1 · Replace circular hit-rate with forward-return metrics
- **Symptom:** Shortlist "hit-rate" is **same-day/circular**: it credits a pick for being up on the
  *same* session it was selected. Two sites compute this.
- **Action:** Replace with **forward** (+1 / +5 session) returns measured *after* the signal date;
  add **per-buy win-rate** and **post-signal max drawdown**. Keep the existing direction-hit harness
  but feed it forward returns, not contemporaneous ones.
- **Anchors:**
  - `src/analysis_store.py:1395-1405` — trailing/circular shortlist hit-rate
    (`ret_1d>0`/`ret_5d>0` on the *current* row).
  - `src/signal_v2_backtest.py:358-377` — same-day `return_1d_pct` direction-hit (uses the same
    session’s realized return as the prediction target).
- **Effort:** M · **Blocked:** `none` (uses already-stored forward rows; may need a date-join helper).
- **Depends on:** none (independent of P0), but most meaningful **after** P0-1/P0-2 so the metric
  scores the fixed shortlist.
- **Done when:** hit-rate reads from `t+1`/`t+5` returns relative to signal date; report shows
  per-buy win-rate and post-signal drawdown; no metric references the signal-day’s own return as the
  outcome.

### P1-2 · Wire news into features at write time + fix negative-only sentiment (code-logic side)
- **Symptom:** News/sentiment isn’t persisted into features at write time (DATA), and the sentiment
  contribution skews negative — drivers effectively only drag, rarely lift.
- **Action (code, here):** Audit the sentiment math and blend so positive news can contribute
  symmetrically. `_news_sentiment_score` (`src/sector_priority.py:1224-1236`) is *formally*
  symmetric (`(pos_hits - neg_hits)/…`), so the negative skew most likely enters via (a) the
  positive-term set being narrower than the negative pattern set (`:370-622` negative patterns;
  positive terms `_POSITIVE_NEWS_TERMS`), and/or (b) the blend weighting `_news_blend_points`
  (`:1558-1559`). Rebalance term lists / blend so a clean positive catalyst yields a net-positive
  `sector_news_score`. The write-time persistence into features is **DATA**.
- **Anchors:** `src/sector_priority.py:1224-1236` (sentiment), `:1289-1320` (`score_sector_news`),
  `:1466-1469` (stock-path sentiment), `:1558-1559` (`_news_blend_points`), `:2152-2171, 2246`
  (blend into rank meta).
- **Effort:** M · **Blocked:** `DATA` for write-time wiring; sentiment-logic rebalance is `none`.
- **Depends on:** none.
- **Done when:** a curated positive-headline fixture yields `sector_news_score > 0` and a positive
  `blend_points`; symmetric negative fixture yields the mirror; unit test covers both signs.

### P1-3 · Implement regime gate (Fix B): gate on FALLING intent, not just low breadth  ← **DONE** (shadow-first)
- **Symptom:** A breadth-only gate (`breadth_above_ema200_pct < ~40%`) would **not** have fired for
  Defence: breadth stayed **80–92%** while `avg_effective_intent_score` fell **54.6 → 41.2 → 40.0**
  (05-31 → 06-03). The deterioration was in *intent momentum*, not breadth level.
- **Action:** Add a regime gate that damps/withholds new buys when **`avg_effective_intent_score` is
  falling** (negative slope over N sessions) **OR** breadth is below a floor — combine the two, don’t
  rely on breadth alone. Source the rollup fields from `sector_daily_rollup` /
  `sector_period_rollup`.
- **Anchors:** consumes sector rollup (`sector_daily_rollup.breadth_above_ema200_pct`,
  `avg_effective_intent_score`); apply at the winner gate alongside the Fix-A penalty in
  `src/sector_priority.py:2114-2135` / winner selection, and/or at `signal_v2` buy gate
  `src/signal_v2.py:604-632`. Phase-1 deferral noted in `PHASE1_FIX_CHECKLIST.md:50-52`.
- **Effort:** M · **Blocked:** `DATA` partial — needs trustworthy sector rollups, which requires the
  sector-tag fix (P1-4) for a correct read.
- **Depends on:** **P1-4** (sector-tag fix) for a complete/correct regime read.
- **Done when:** on the Defence 05-31→06-03 window the gate fires from the falling-intent branch
  (breadth still high), damping new Defence buys; a breadth-only control does **not** fire (proving
  the slope branch is what catches it).

### P1-4 · Fix sector-tag mismatch corrupting rollups (DATA-owned; code dependency note)
- **Symptom:** Symbol→sector tag mismatches corrupt `sector_daily_rollup` / `sector_period_rollup`,
  so breadth/intent aggregates per sector are wrong.
- **Action:** **DATA agent** owns the mapping fix across `sector_catalog` / `instrument_sector_map`.
  Here: note that **P1-3 and P1-6 read these rollups**, so they must consume the corrected mapping.
- **Anchors:** mapping tables `instrument_sector_map`, `sector_catalog`; consumers = P1-3 regime
  gate + P1-6 reconciliation.
- **Effort:** M (DATA) · **Blocked:** `DATA`.
- **Depends on:** none. **Blocks:** P1-3, P1-6.
- **Done when:** each instrument resolves to exactly one sector and per-sector counts reconcile to
  membership; spot-check Defence roster matches the rollup denominator.

### P1-5 · Backfill `session_move_vs_prev_close_pct` (DATA-owned; unblocks Fix C + P0-2)
- **Symptom:** The same-session move field is sparse, weakening Fix C’s de-bias and the P0-2
  sign-gate.
- **Action:** **DATA agent** backfills `session_move_vs_prev_close_pct`. Here: note it is the clean
  input for P0-2’s down-day sign-gate and for the already-shipped Fix C dampener (`sector_audit.py`).
- **Anchors:** consumed by Fix C in `src/sector_audit.py`; consumed by P0-2 sign-gate
  `src/sector_priority.py:2114-2135`.
- **Effort:** S–M (DATA) · **Blocked:** `DATA`.
- **Depends on:** none. **Improves:** P0-2 (fallback is `return_1d_pct`), Fix C accuracy.
- **Done when:** field populated for the working window; P0-2 sign-gate uses it instead of the
  `return_1d_pct` fallback.

### P1-6 · Reconcile the two scoring systems (gate winners by `signal_v2` risk labels)  ← **DONE** (shadow-first)
- **Symptom:** The **offensive** winners buy-list ignores the **defensive** `signal_v2`: of 10
  Defence buy picks, `signal_v2` labeled **0 buy, 5 trim/exit** (incl. #1 **BDL**). The defensive
  engine was **73–89% correct**; the offensive list is what fails.
- **Action:** Before a name is published as a winner/buy, cross-check its `signal_v2` label and
  **down-gate or drop** picks labeled `trim`/`exit-risk` (and optionally require not-worse-than
  `accumulate`). I.e. winners shortlist becomes the intersection of momentum rank AND v2 risk
  clearance.
- **Anchors:** winners producer/persist `src/sector_priority.py:2138+`, `:2440-2519`; v2 labels via
  `_map_label`/`_buy_gate` `src/signal_v2.py:604-647`. Backtest cross-eval harness
  `src/signal_v2_backtest.py:350-384`.
- **Effort:** M · **Blocked:** depends on P0-3 (v2 inputs persisted) and P1-4 (clean sector read).
- **Depends on:** P0-3, P1-4 (and best run after P0-1 refresh).
- **Done when:** a replayed winners list excludes/down-ranks every name `signal_v2` calls
  `trim`/`exit-risk` (BDL no longer publishable as a buy), and the published list’s forward win-rate
  (P1-1) improves vs the un-gated list.

---

## P2 — Robustness

### P2-1 · Thin-history guard
- **Symptom:** Names with tiny history (**MIDHANI 8 rows, BEML 18, AI 12–15**) produce NaN
  `ema200_stretch_atr` and unreliable scores, yet can still rank.
- **Action:** Add a `history_lt_200_sessions` guard that ceilings such names to **hold** (cap the
  constructive side, never block downgrades) when EMA200-dependent features are NaN due to short
  history.
- **Anchors:** stretch NaN origin `src/sector_priority.py:2087-2111` (returns NaN when <200 EMA
  history); ceiling application pattern `signal_v2._apply_ceiling` `src/signal_v2.py:650-653`.
- **Effort:** S–M · **Blocked:** `none`.
- **Depends on:** none.
- **Done when:** symbols with <200 sessions get a `history_lt_200_sessions` flag and a `hold`
  ceiling; MIDHANI/BEML/AI cannot be published as buys on NaN stretch.

### P2-2 · De-weight/gate options corroborators when unavailable
- **Symptom:** Option chain is unavailable **84–93%** of the time, so Layer-B Tier-2 options
  corroborators are effectively dead/noisy.
- **Action:** Cleanly **gate** the options corroborator: when chain data is unavailable, set its
  weight to 0 (don’t let "missing" leak as a weak signal) rather than partially counting it.
- **Anchors:** Layer-B/Tier-2 corroborator inputs in `signal_v2` (Layer B/D family handling around
  `src/signal_v2.py:285-282` family block and Layer-D modifiers `:403+`); locate the options
  availability flag and short-circuit its contribution.
- **Effort:** S · **Blocked:** `none`.
- **Depends on:** none.
- **Done when:** with option chain absent, the options term contributes exactly 0 and is marked
  `unavailable` in the trace (not a bear/bull nudge).

### P2-3 · Doc/schema drift
- **Symptom:** Docs reference **non-existent** `analysis_rollups` and `sector_registry`; the real
  schema is `sector_daily_rollup` + `sector_period_rollup` and
  `sector_catalog` / `market_instruments` / `instrument_sector_map`.
- **Action:** Update docs to the actual table names so future work (esp. P1-3/P1-4/P1-6) references
  real objects.
- **Anchors (verified):** stale references in `docs/TITAN_FRAMEWORK_DEEP_DIVE.md`,
  `docs/STEP4_SECTOR_CLEANUP_2026-05-18.md`,
  `docs/NSE_SECTOR_PRIORITY_RECONCILIATION_2026-05-18.md`, `docs/TITAN_BUILD_AND_DEPLOY.md`,
  `docs/AI_SECTOR_FIX_AUTOMATION_REPORT_2026-05-18.md`; align with
  `docs/INSTITUTIONAL_FLOW_ROADMAP.md`.
- **Effort:** S · **Blocked:** `none`.
- **Depends on:** none.
- **Done when:** grep for `analysis_rollups`/`sector_registry` in `docs/` returns no live
  references; replaced with the actual table names.

---

## New data feeds (owned by the data-pipeline agent)

These are **not** code tasks here. Each line lists the feed and the **code consumer it unblocks** so
the implementation order is clear. Titan can scrape all of these free from official NSE EOD reports
(same pattern already used).

| Feed | Unblocks (code consumer) |
| --- | --- |
| **Delivery %** (NSE `sec_bhavdata_full`) | Churn/low-conviction gate on winners + buy gate (catches DIXON/CANBK-type misses Fix A can’t) |
| **F&O ban-list + circuit/price-band** | Hard buy-withhold veto in winners + `signal_v2` buy gate |
| **Futures OI / basis / rollover** | Short-covering-pop vs long-buildup flag → tempers absorption (P0-2) and momentum terms |
| **FII/DII cash + institutional_flow (FII derivatives)** | Activate dormant institutional-flow block; feeds regime gate (P1-3) |
| **India VIX** | Regime gate (P1-3) volatility branch + position-size context |
| **Results / corp-actions calendar** | Event blackout filter on buys; explains gaps in forward returns (P1-1) |
| **Valuation-vs-history percentile + promoter-pledge** | Distress/expensive filter complementing overextension (Fix A / P0-2) |

**Done when (for each feed):** column(s) persisted at feature/rollup write time with coverage on the
working window, and the named consumer reads it behind an env flag with a NaN-safe fallback.

---

## Recommended IMPLEMENTATION ORDER (single sequence)

1. **P0-2** — cap + sign-gate absorption term (top code priority; uses `return_1d_pct` fallback so it
   isn’t blocked). *(code, none)*
2. **P1-5** (DATA) — backfill `session_move_vs_prev_close_pct` → upgrade P0-2’s sign-gate input.
3. **P0-3** — derive/consume `ema200_stretch_atr` + read v2 risk-gate inputs *(code; DATA backfills
   in parallel)*.
4. **P1-4** (DATA) — fix sector-tag mapping (blocks P1-3 & P1-6).
5. **P0-1** — re-run rankings + persist winners so the live shortlist carries Fix A/C + P0-2.
   *(Breeze + DATA)*
6. **P1-6** — gate winners by `signal_v2` risk labels (needs P0-3, P1-4; best after P0-1).
7. **P1-3** — regime gate on falling intent + breadth floor (needs P1-4).
8. **P1-1** — forward-return / per-buy win-rate / drawdown metrics (score the now-fixed list).
9. **P1-2** — sentiment rebalance + (DATA) write-time news wiring.
10. **P2-1** — thin-history hold ceiling.
11. **P2-2** — gate options corroborator when unavailable.
12. **P2-3** — doc/schema drift cleanup.
13. **New data feeds** — delivery% and F&O ban/circuit first (highest signal for the named misses),
    then futures OI, FII/DII, VIX, calendar, valuation/pledge — each enabling its consumer above.

> Rationale for ordering: P0-2 is the single biggest miss-driver and is unblockable today. Data
> backfills (P1-5, P0-3 upstream, P1-4) run in parallel and gate the evaluation/reconciliation work.
> The winners refresh (P0-1) sits after the absorption fix so the live list is correct on first
> republish, and the forward-return metrics (P1-1) come last among P1 so they measure the fixed
> system, not the broken one.

---

## FINAL VALIDATION (2026-06-14) — branch `14thJune`, read-only Supabase

Agent: validation subagent. No code committed; only this checklist + `VALIDATION_SUMMARY.md` written.

### Completed (validated this run)

- [x] **Fix A** — overextension penalty (replay: 4/8 fallers demoted; GARFIBRES/MAHABANK protected)
- [x] **Fix C** — contemporaneous de-bias (buy-rating: −8 OFF-buys removed, avg fwd-5d of removed −1.21%)
- [x] **P0-2** — absorption cap + down-day sign-gate (30 spurious down-day bonuses zeroed; BDL +62.8→0)
- [x] **P1-3** — regime gate on falling intent (Defence/Telecom/banks_psu hostile @ 2026-06-03)
- [x] **P1-6** — v2 risk-label gate (31 names would-withhold; BDL/MTARTECH/COCHINSHIP in bottom fwd-5)
- [x] **Forward-return harness** — `scripts/forward_return_eval.py` + `validate_fix_forward_returns.py` +
  `replay_12_stocks_ac.py` all ran clean (exit 0)
- [x] **EOD feeds** — 21 delivery dates (~2.6k rows/day), 20 futures dates (~216/day), 20 VIX, 17 ban
  dates; institutional_flow 1 row (2026-06-12); corp-actions 147 rows / 19 ex-dates
- [x] **Shadow gates** — default shadow = 0 withhold / 0 multiplier change (`val_modes.py`); enforced
  preview (`val_combined.py`) improves decline mix on named-12 (67%→50%) and AI (33%→0% on 1 kept)

### Still pending (not validated / not done)

- [ ] **P0-1** — refresh stale winners shortlist (`sector_daily_winners.as_of_date` still **2026-05-30**)
- [ ] **P0-3** — v2 risk-gate input derivation + DATA backfill
- [ ] **P1-1** — wire forward-return metrics into `analysis_store` / backtest (harness exists; consumers not swapped)
- [ ] **P1-4 / P1-5** — sector-tag fix + `session_move_vs_prev_close_pct` backfill (DATA)
- [ ] **Live-streaming Phase 1 enforce** — flip gates from shadow → damp/skip in production (`TITAN_LIVE_DATA_PLAN.md` P3-a)
- [ ] **Promoter-pledge + SLB borrow-trend** distress flags (`PHASE1_FIX_CHECKLIST.md` Phase 3)

### Headline numbers (see `VALIDATION_SUMMARY.md`)

| Cohort | Metric | Baseline (fix-OFF / no gates) | Current (fix-ON / enforced gates) |
| --- | --- | --- | --- |
| Twelve stocks | cohort fwd+5 win% / avg | 48.6% / +0.34% | (stored signals; gate sim on 06-01 pick: 67%→50% decline) |
| defence+ai+telecom | cohort fwd+5 win% / avg | 37.7% / −0.54% | enforced: defence 0 published; AI 1 kept @ +4.64% fwd5 |
| Top-3 sector winners | avg fwd-5d (Fix A ON vs OFF) | −1.04% | −0.96% (+0.08 pp) |
| Buy-rated (all target) | count / avg fwd-5d | 64 / +0.94% | 56 / +1.24% (+0.31 pp) |

### Live-run blockers

1. **Stale winners** — `sector_daily_winners` / `sector_priority_rankings` stuck at `2026-05-30`; fixes
   are code-only until P0-1 Breeze re-run persists new `score_breakdown` + shadow gates.
2. **Shadow mode** — all new gates default `shadow`; no buy-suppression is live until enforcement flip
   after measurement sign-off.
3. **Breeze session** — P0-1 refresh requires live Breeze (explicitly out of scope for this validation).
4. **Sector-tag rollup quality** — defence shows only 1 member @ 2026-06-12 in features (P1-4 DATA);
   regime gate reads may be incomplete until mapping fix.
5. **FII/DII history** — `institutional_flow` has 1 date only; institutional context gate is thin.
6. **Over-blocking risk** — enforced gate stack blocks 7/8 named-12 fallers but also 3 risers
   (CANBK/GREAVESCOT/PNB); not ready to enforce without tighter tuning.
