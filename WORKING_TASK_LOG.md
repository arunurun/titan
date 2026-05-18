# Working Task Log

## 2026-05-18

- Built `ai` sector priority ranking pipeline with persisted table and refresh scripts.
- Added market-cap source fallbacks and issue diagnostics (no silent null handling).
- Created and populated `sector_priority_rankings` for `ai`.
- Added `--sector-priority-only` / `--sector-priority-top-n` run path.
- Created and wired `sector_daily_winners` persistence for daily top-10 picks.
- Fixed idempotency for same-day winner refresh persistence.
- Investigated Screener/Moneycontrol/NSE data-access paths and documented blockers.
- Audited `ai` sector membership and identified clear non-AI symbols.
- Executed first curation pass to remove clear non-AI names from `ai`.
- Executed strict `ai` allowlist curation (core + adjacent only).
- Re-ran `ai` ranking/winner refresh after strict curation.
- Re-validated post-cleanup `ai` top-10 winners and issue distribution.
- Switched strict `ai` allowlist to deep-scan Top-12 candidates and refreshed winners.
- De-duplicated active AI mappings to NSE-preferred single-exchange rows.
- Fixed stale same-day symbol leakage in `symbol_daily_features` rollup inputs.
- Finalized AI universe to 12 stocks and regenerated top-10 winners + Titan digest.

## 2026-05-17

- Stabilized sector runs with NSE-first instrument normalization.
- Added exchange fallback (`NSE` <-> `BSE`) for equity data fetches.
- Added sector audit visibility for exchange fallback usage.
- Added live validation scripts for sector-wide and pull-only checks.
- Hardened Breeze handling for `Historical Data Fail` responses.
- Added call pacing + rate-limit-aware retries for Breeze historical endpoints.
- Ran focused test suites for registry, Breeze client, and sector audit changes.

