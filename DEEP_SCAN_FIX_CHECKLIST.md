# Titan Deep-Scan Fix Checklist — Implement ASAP

Source: read-only deep diagnostic of **Defence** (15 symbols) & **AI** (12 symbols) data + logic
against live Supabase on 2026-06-14. Builds on `PHASE1_FIX_CHECKLIST.md` and
`docs/INSTITUTIONAL_FLOW_ROADMAP.md`. No code/DB/git changes made by the scan.

Priority key: **P0** = must fix before trusting any "buy"; **P1** = materially distorts
evaluation; **P2** = quality / robustness.

---

## P0 — Blockers (do first)

- [ ] **P0-1 — Refresh the stale, pre-fix winners shortlist.**
  - Symptom: `sector_priority_rankings` + `sector_daily_winners` for both sectors stuck at
    `as_of_date = 2026-05-30` while features are fresh to `2026-06-12`; every row has
    `overextension_penalty = None` and `rank_score == technical_rank_score`.
  - Action: restore a valid Breeze session and re-run `scripts/refresh_sector_daily_winners.py` /
    `scripts/refresh_sector_priority_rankings.py` so the shortlist is current **and** carries the
    already-coded Fix-A penalty.
  - Done when: latest `as_of_date` ≈ current session and `overextension_penalty` is non-null.

- [ ] **P0-2 — Cap AND sign-gate the absorption term in `rank_score` (single biggest driver of misses).**
  - Symptom: `sector_priority.py:~2110` `absorption_term = (absorption - 1.0) * 8.0` is **uncapped
    and sign-blind** — a single high-volume day dominates the rank regardless of whether price rose
    or *fell*. Backtest reproduced the stored scores exactly: BDL crowned **#1 Defence (51.31)** off
    an 8.85× volume **−6% DOWN day** → absorption = **+62.8 pts = 122% of its score**, while 1w/1m
    momentum was negative and `signal_v2` said `exit-risk` every session (realized −4.6% EOW). HAL
    absorption = 104% of score, BEML = 114%. NETWEB #1 AI off a 9.25× climax up-day that gave the
    pop back to +2.9% EOW.
  - Action: **cap** the absorption contribution **and zero/invert it on down-days** (sign-gate on
    same-session direction); expose cap/weight as `TITAN_*` env knobs. This is NOT in the current
    Fix A/C scope and is the dominant Defence failure.
  - Done when: a high-volume down-day can no longer top the buy list; BDL/HAL/BEML demoted.

- [ ] **P0-3 — Persist the v2 risk-gate inputs so the signal is reproducible/backtestable.**
  - Symptom: `signal_v2` consumes `cmf_20`, `obv_slope_20`, `adx_14`, `adx_plus_di_14`,
    `ema200_stretch_atr`, `sector_pctile_ema200_stretch` — none stored in `symbol_daily_features`
    or `tape_extras`. `signal_v2_backtest.feature_row_to_audit` (`:171`) reads them, finds nothing,
    and silently recomputes labels with money-flow / over-extension / ADX-regime layers = 0.
  - Action: persist `cmf_20 / adx_14 / adx_plus_di_14 / obv_slope_20` into
    `symbol_daily_features` (or `tape_extras`); derive `ema200_stretch_atr` from stored
    `ema_200_distance_pct / atr_14_pct` inside `feature_row_to_audit`.
  - Done when: a 12-stock replay reproduces production labels (Layers C/C-8/D non-zero).

---

## P1 — Evaluation integrity (do next)

- [ ] **P1-1 — Replace circular hit-rate with forward (+1/+5) returns.**
  - Symptom: `analysis_store.py:1395-1405` scores shortlist "hits" on **trailing** returns of
    momentum-selected names (tautological); `signal_v2_backtest.py:358-376` scores `direction_hit`
    against **same-day** `return_1d_pct`. Forward fields (`reconcile_next_week_hit`) are sparse
    (14/93 Defence, 57/149 AI).
  - Action: persist forward outcome per signal; compute non-circular buy hit-rate, per-buy win-rate,
    and post-signal drawdown.

- [ ] **P1-2 — Wire news into features at write time + fix negative-only sentiment.**
  - Symptom: `symbol_news_snapshots` (5,071 rows, fresh to 06-12, real coverage) but
    `symbol_daily_features.news_count/news_sentiment_score` are 0 in ~87% (Defence) / ~97% (AI) of
    recent rows. Snapshots written ~13:01 IST, likely after the feature run. Every snapshot
    `aggregate_score` is negative (−0.07..−0.70), so the `_news_blend_points` (weight 3.5) can only
    subtract.
  - Action: join snapshots into the feature write; fix `_news_sentiment_score` so positive news can
    confirm, not just veto.

- [ ] **P1-3 — Implement the regime gate (Fix B) — gate on FALLING avg intent, not just low breadth.**
  - Symptom: `sector_daily_rollup.breadth_above_ema200_pct` / `avg_effective_intent_score` are
    populated but no code path damps new buys. Backtest: Defence `avg_effective_intent_score` fell
    **54.6 → 41.2 → 40.0** (05-31→06-03) — a clear rollover the ranker ignored while issuing 20
    buys into net-negative sectors. Breadth stayed 80–92% (a *stretched* condition, not healthy), so
    the deferred `breadth < 40%` gate would **not** have fired — the effective gate here is **falling
    avg intent**. `symbol_count` per day swings 1→23 and excludes mis-tagged members.
  - Action: add a buy-damping consumer keyed on falling `avg_effective_intent_score` (plus the
    breadth floor); first fix the tag mismatch below so the regime read is complete.

- [ ] **P1-6 — Reconcile the two scoring systems: winners buy-list ignores `signal_v2`.**
  - Symptom: of the 10 Defence "buy" winner picks, `signal_v2` labeled **0 as buy** and 5 as
    `trim`/`exit-risk` (including #1 BDL). `sector_priority.rank_score` and `signal_v2` risk_net
    never reconcile, so the actionable shortlist contains names the engine itself would refuse. The
    defensive `action_signal` engine was actually accurate (trim/exit 73–89% correct); the offensive
    list is what fails.
  - Action: gate the winners shortlist by `signal_v2` risk labels (drop/demote names the engine
    flags `trim`/`exit-risk`) so the buy list and risk engine agree.

- [ ] **P1-4 — Fix sector-tag mismatch corrupting rollups.**
  - Symptom: `symbol_daily_features.sector` tags **BEML → `railways_transport_infra`** and
    **E2E → `data_centre`**, though the registry maps them to defence/ai. `sector_daily_rollup`
    keys on the feature tag, so these names are silently excluded from their own sector aggregates.
  - Action: reconcile feature `sector` tags with `sector_registry`; rebuild rollups.

- [ ] **P1-5 — Backfill `session_move_vs_prev_close_pct` (unblocks Fix C).**
  - Symptom: present in only ~19% (Defence) / ~30% (AI) of `tape_extras`, so the checklist's
    same-day de-bias (Fix C) cannot run.
  - Action: backfill consistently, then implement the Fix-C discount.

---

## P2 — Robustness

- [ ] **P2-1 — Thin history for new names** (MIDHANI 8 rows, BEML 18, most AI 12–15) forces
      `history_lt_200_sessions` → `ceiling=hold` and unstable percentiles; live 90-day Breeze fetch
      leaves `ema200_stretch_atr` NaN. Add a minimum-history guard / fallback.
- [ ] **P2-2 — Options corroborators near-dead** for these sectors (option chain unavailable
      84–93%); Layer-B Tier-2 rarely contributes. De-weight or gate cleanly when unavailable.
- [ ] **P2-3 — Doc/schema drift:** docs/prompts reference non-existent `analysis_rollups` /
      `sector_registry` tables (actual: `sector_daily_rollup` + `sector_period_rollup`, and the
      `sector_catalog` + `market_instruments` + `instrument_sector_map` trio). Update docs.

---

## New data feeds to add (all free from NSE EOD) — larger effort, schedule after P0/P1

- [ ] **Delivery % / delivery qty** (`sec_bhavdata_full`) — replaces the broken unbounded "absorption"
      proxy; distinguishes accumulation from intraday churn. **High.**
- [ ] **F&O ban list + circuit / price-band** — hard veto on locked/undeliverable names (defence
      small-caps hit bands often). **High.**
- [ ] **Futures OI / basis / rollover** — short-covering pop vs genuine long buildup for F&O names
      (HAL, BEL, BDL, PERSISTENT, KPITTECH). **High.**
- [ ] **FII/DII cash + FII derivative stats** — activate the dormant `institutional_flow` block
      (`docs/INSTITUTIONAL_FLOW_ROADMAP.md`). **Medium-High.**
- [ ] **India VIX / regime series** — volatility-aware buy gating; complements Fix B. **Medium.**
- [ ] **Results & corporate-actions calendar** — event guardrail (currently
      `event_guardrail_count = 0` everywhere). **Medium.**
- [ ] **Valuation-vs-history percentile, promoter-pledge** — distress / overvaluation filters
      (Phase 3). **Low-Medium.**

---

## Key code references

- `src/sector_priority.py:2099-2120` (scoring + uncapped absorption), `:2005-2069` (Fix-A penalty,
  never persisted), `:2425-2505` (winners persistence)
- `src/signal_v2.py:285-395` (Layer-C inputs not stored), `:604-647` (buy gate)
- `src/signal_v2_backtest.py:171-204` (audit rebuild gap), `:358-376` (same-day hit-rate)
- `src/analysis_store.py:1362-1405` (trailing/circular shortlist hit-rate)
