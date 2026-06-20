# Action Label Backfill Report (P0)

**Branch:** `20thJune`  
**Generated:** 2026-06-20  
**Window:** 2026-05-01 – 2026-06-20 (May–Jun)  
**P1 commit:** `d2ee974b89fe2384459b124526af428bb286b5fd`

## Summary

| Metric | Value |
|--------|------:|
| **Rows backfilled (this run)** | **9** (5 first pass + 4 second pass) |
| Pre-backfill mismatches (dry-run, post-P1) | 3 → 5 after first live pass |
| Post-backfill mismatches (dry-run) | **0** |
| Failures | 0 |
| Scope | all-stocks |

## Script / path used

- **Primary:** `scripts/backfill_action_labels.py`
  - Env: `TITAN_ENABLE_ANALYSIS_STORE=1`, `TITAN_RECONCILE_MODE=1`
  - Command:
    ```bash
    python scripts/backfill_action_labels.py --start 2026-05-01 --end 2026-06-20
    ```
  - Second idempotent pass cleared prior-chain `buy→accumulate` residuals (4 rows).
- **Core logic:** `analysis_store.persist_action_label_backfill()` — Supabase-only recompute via `signal_v2_backtest.recompute_label(use_v2=True)` with per-symbol `prev_action_signal` chain.

## Label transitions (this run)

| Transition | Count |
|------------|------:|
| hold → trim | 4 |
| buy → accumulate | 4 |
| accumulate → buy | 2 |
| hold → accumulate | 1 |

## Notes

- Backfill patches `symbol_daily_features.action_signal` and `tape_extras.sell_signal` only (no Breeze / live fetch).
- Prior bulk realign (pre-P1, ~3407 rows) already cleared most May–Jun stale labels; this run realigned **9 rows** after P1 signal tuning (`d2ee974`).
- Residual `buy→accumulate` chain sensitivity cleared by **second idempotent pass**; final dry-run: **0 mismatches**.

## DB credentials

- Supabase creds present in local `.env` — **no blocker**; live run succeeded.

## Related paths

| Component | Path |
|-----------|------|
| Analysis store persist | `src/analysis_store.py` |
| Reconcile runner | `src/reconcile_runner.py` |
| Post-market CLI | `scripts/run_post_market_reconcile.py` |
| Backfill CLI | `scripts/backfill_action_labels.py` |
