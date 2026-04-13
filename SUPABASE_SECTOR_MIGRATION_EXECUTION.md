# Supabase Sector Migration - Execution Tracker

## Status legend

- `NOT STARTED`
- `IN PROGRESS`
- `COMPLETED`
- `BLOCKED`

## Execution steps


| Step | Task                                                                                                             | Status    | Notes                                                                                                             |
| ---- | ---------------------------------------------------------------------------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------- |
| 1    | Create granular execution tracker for this migration                                                             | COMPLETED | This file is the source of truth for run status.                                                                  |
| 2    | Add Supabase SQL schema for sector catalog, instruments, mapping, overrides, and scanner run logs                | COMPLETED | Added `sql/create_sector_registry_tables.sql`.                                                                    |
| 3    | Refactor Titan sector registry to load sector instruments from Supabase (replace CSV read path)                  | COMPLETED | `src/sector_registry.py` now queries Supabase mappings instead of CSV files.                                      |
| 4    | Add Titan fallback behavior and explicit errors for missing/empty Supabase sector data                           | COMPLETED | Added explicit missing-env, missing-table, and empty-sector errors.                                               |
| 5    | Update and expand tests for new Supabase-backed registry behavior                                                | COMPLETED | Rewrote `tests/test_sector_registry.py` to mock Supabase query flow.                                              |
| 6    | Add migration/backfill helper to seed Supabase from existing CSV sector files                                    | COMPLETED | Added `scripts/backfill_sector_registry_from_csv.py`.                                                             |
| 7    | Scaffold separate provider-sync codebase inside a standalone folder for eventual separate GitHub repo            | COMPLETED | Added isolated `provider-universe-sync/` package with independent config/deps.                                    |
| 8    | Implement weekly NSE+BSE sync pipeline in provider codebase with hybrid sector assignment (official + overrides) | COMPLETED | Added provider fetch + sector resolver + Supabase sync pipeline.                                                  |
| 9    | Add weekly GitHub Actions workflow (Sunday 07:00 IST) for provider repo                                          | COMPLETED | Added `provider-universe-sync/.github/workflows/weekly-universe-sync.yml` with `30 1 * * 0`.                      |
| 10   | Update Titan docs to reflect Supabase sector source and provider separation                                      | COMPLETED | Updated `main.py` CLI text and `TITAN_SECTOR_ANALYSIS_PLAN.md` references.                                        |
| 11   | Run targeted tests and sanity checks                                                                             | COMPLETED | Ran `pytest tests/test_sector_registry.py` and `python -m compileall src scripts provider-universe-sync/src`.     |
| 12   | Final review and handoff summary with any blockers                                                               | COMPLETED | Resumed after user approval to keep concurrent edits as-is; no new blockers for migration track. |


## Production rollout run log

| Rollout step | Status | Notes |
|---|---|---|
| R1: Validate runtime credentials from `.env` | COMPLETED | Supabase/Breeze/Gemini vars are present in `.env`. |
| R2: Backfill sector registry to Supabase | COMPLETED | Backfill succeeded: `defence` rows=27, total_links=27. |
| R3: Run provider weekly sync manually | IN PROGRESS | Patched sync to batched upserts + retry and rerunning manual sync. |
| R4: Titan sector smoke run against Supabase | NOT STARTED | Pending successful R3 completion. |

