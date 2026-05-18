# AI Sector Quality & Automation Fix Report (2026-05-18)

## 1) Objective and constraints

### Objective
- Restore AI sector output quality so daily winners are generated from a curated AI-only universe (remove obvious non-AI contamination).
- Make sector processing operationally reliable (no silent failures, clear issue flags, repeatable refresh flows, idempotent persistence).
- Build a reusable pattern to scale this from AI pilot to additional sectors.

### Constraints observed during execution
- Breeze historical API is noisy and can return `No Data Found` / `Historical Data Fail` in HTTP-200 responses.
- Session expiry can interrupt long multi-sector runs (`[Breeze] Session token expired` surfaced in live validation report).
- Exchange mapping quality is mixed; many symbols have NSE/BSE duplicates, and one side may have no usable history.
- Sector registry and analytics depend on Supabase table availability and REST exposure.
- Requirement to avoid destructive repo actions and keep automation idempotent for same-day reruns.

---

## 2) Timeline of actions (chronological, with rationale)

| Time window | Action | Rationale | Outcome |
|---|---|---|---|
| Morning start | Built AI sector priority ranking pipeline and persistence model (`sector_priority_rankings`) | Move from ad-hoc symbol picks to scored, reproducible ranking | Ranking rows persisted with score/meta/issue diagnostics |
| Morning | Added multi-source market-cap retrieval (NSE -> Moneycontrol -> Screener -> Yahoo -> prior snapshot fallback) | Prevent missing cap from breaking ranking quality | Reduced null-cap situations and exposed source provenance in metadata |
| Morning | Added CLI path `--sector-priority-only` and `--sector-priority-top-n` | Ensure production sector run can consume curated priority list directly | AI runs can operate from persisted top-N instead of full noisy universe |
| Morning | Added `sector_daily_winners` table + winner refresh workflow | Persist daily top picks for auditability and downstream digest use | Daily top-10 winners are stored with breakdown and issue flags |
| Morning-mid | Fixed idempotency for same-day winner refresh | Re-running on same day previously risked stale/duplicate behavior | Added delete-then-upsert by sector/date before winner write |
| Midday | Investigated NSE/Moneycontrol/Screener data-access paths + blocker behaviors | Understand where quality loss enters (source outages, parse misses) | Failure reasons are explicitly captured per symbol |
| Midday | Audited AI membership and removed clear non-AI names | Universe contamination was primary root cause for poor winner quality | First curation pass completed |
| Midday-late | Applied strict allowlist curation (core + adjacent AI names) and moved excluded rows to `unknown` with override reason | Enforce deterministic AI universe policy | AI universe tightened and documented through `sector_overrides` |
| Late afternoon | Re-ran AI ranking and winner refresh after strict curation | Validate that cleaned universe changes top picks materially | Winner set regenerated using curated membership |
| Late afternoon | Re-validated top-10 and issue distribution post-cleanup | Confirm data quality and scoring health after curation | Cleaner issue profile and stable top-10 generation |
| Late afternoon | Switched strict allowlist to deep-scan Top-12 candidates, deduped to NSE-preferred active mappings | Keep 10 active + 2 backup structure while avoiding duplicate exchanges | Final active AI universe = 12 symbols |
| End-of-day | Fixed stale same-day symbol leakage in `symbol_daily_features` rollup inputs | Multiple runs in same day were contaminating leaders/laggards context | Same-day sector slice is replaced before upsert |
| End-of-day | Finalized AI universe and regenerated top-10 winners + digest | Lock in production-ready AI pilot state | AI flow now curated, ranked, persisted, and repeatable |

---

## 3) Data issues found and root causes

### Issues found
- **Sector contamination:** AI sector contained several non-AI names, degrading relevance of winners.
- **Exchange duplication noise:** Same symbol mapped on both NSE/BSE created inconsistent fetch outcomes.
- **Breeze no-data instability:** Historical endpoint returned empty/no-data for valid mapped symbols.
- **Session interruption:** Multi-sector validations were cut by session token expiry.
- **Same-day stale feature leakage:** Repeated runs could blend stale symbols into rollup comparisons.
- **Missing market-cap fields:** Single-source cap fetch produced gaps and poor bucketing/scoring.

### Root causes
- **Registry curation gap:** AI mapping had broad or stale sector assignments without strict policy enforcement.
- **Data-provider behavior:** Breeze returns soft failures (`No Data Found`, `Historical Data Fail`) requiring explicit handling.
- **Universe normalization gap:** No strict primary-exchange normalization before curation/ranking.
- **Idempotency gap in analytics:** Prior same-day writes did not fully replace sector slice in `symbol_daily_features`.
- **Fallback gap for cap:** Reliance on one cap source caused null propagation into ranking features.

### Evidence highlights
- `data/reports/live_sector_validation.json` showed AI `requested=45`, `successful=28`, `skipped_no_data=17`, and widespread run disruption after session expiry for many sectors.
- AI curation script established deterministic include/exclude policy and generated override rows for traceability.

---

## 4) Code/database changes made (files, tables, scripts)

### Core code changes

| File | Change | Why it matters |
|---|---|---|
| `src/sector_priority.py` | New ranking engine, cap-source fallback chain, score computation, priority flagging, persistence loaders/writers, daily winners persistence | Foundation for deterministic AI ranking + top-N winner generation |
| `src/sector_registry.py` | NSE-first primary exchange normalization (`_normalize_primary_exchange`) | Reduces duplicate/no-data symbol noise |
| `src/breeze_client.py` | Soft-no-data handling, rate-limit aware retry/backoff, exchange fallback NSE<->BSE, metadata attrs (`exchange_used`, `exchange_fallback_used`) | Improves run resilience and diagnostics |
| `src/sector_audit.py` | Priority-only load path, quality gate, guardrails, calibrated absorption, sell-signal scaffolding, digest diagnostics | Makes sector automation actionable with explicit risk controls |
| `src/analysis_store.py` | Delete-then-upsert same-day sector slice in `symbol_daily_features` | Prevents stale same-day leakage in rollup/comparison context |
| `main.py` | CLI support for priority-only runs and top-N priority cap | Enables production invocation of curated AI priority workflow |

### SQL changes

| SQL file | Table created | Purpose |
|---|---|---|
| `sql/create_sector_priority_rankings.sql` | `public.sector_priority_rankings` | Persist full daily sector ranking + priority flag |
| `sql/create_sector_daily_winners.sql` | `public.sector_daily_winners` | Persist daily winner list with score/issue metadata |

### Scripts added for operations

| Script | Purpose |
|---|---|
| `scripts/refresh_sector_priority_rankings.py` | Generic ranking refresh (pilot defaults to `ai`) |
| `scripts/refresh_ai_daily_winners.py` | AI-specific ranking + winner refresh |
| `scripts/curate_ai_sector.py` | Strict AI universe curation + override writes |
| `scripts/validate_all_sectors_live.py` | End-to-end sector digest validation report |
| `scripts/validate_data_pull_only.py` | Pull-only health validation (fetch coverage/no-data/fallback stats) |

---

## 5) Supabase curation logic and final AI universe criteria

### Curation logic implemented
- Resolve sector ids for `ai` and `unknown` from `sector_catalog`.
- Read active AI mappings from `instrument_sector_map` + `market_instruments`.
- Enforce strict allowlist membership for AI; any active AI instrument outside allowlist is deactivated for AI and moved to `unknown`.
- Upsert `sector_overrides`:
  - excluded symbols -> `sector_key="unknown"` with reason payload (`ai_curation_v1`, `exclude_from_ai`)
  - included allowlist symbols -> `sector_key="ai"` with reason payload (`ai_curation_v2_deepscan`, `include_in_ai`)
- Canonicalize allowlist to one active mapping per symbol with NSE preference.

### Final AI universe criteria (implementation-ready)
- **Primary criterion:** Symbol must be in strict AI allowlist.
- **Exchange criterion:** Keep one active row per symbol; prefer NSE over BSE.
- **Size criterion:** Maintain 10 active AI candidates + 2 backups for replacement continuity.
- **Traceability criterion:** Every forced include/exclude must exist in `sector_overrides` with machine-readable reason JSON.

### Final strict universe (12)
- Active/core set: `E2E`, `SUBEXLTD`, `GENESYS`, `MOSCHIP`, `HAPPSTMNDS`, `DATAMATICS`, `TANLA`, `NETWEB`, `PERSISTENT`, `AFFLE`
- Backup set: `KPITTECH`, `TATAELXSI`

---

## 6) Ranking/winner methodology and quality gates

### Ranking methodology (`sector_priority_rankings`)
- Inputs per symbol:
  - `return_1w_pct` (5 sessions),
  - `return_1m_pct` (20 sessions),
  - `absorption_ratio` (volume participation proxy),
  - `market_cap_bucket` from `market_cap_inr_cr`.
- Market-cap source chain:
  1. NSE quote API
  2. Moneycontrol scrape
  3. Screener scrape
  4. Yahoo quote API
  5. prior persisted snapshot fallback
- Scoring formula:
  - `rank_score = cap_bias + 1.1*ret_1w + 0.45*ret_1m + 8*(absorption-1)`
  - cap bias favors smaller caps (`micro > small > mid > large`).
- Priority selection:
  - Rank descending by score.
  - Candidate filter requires non-zero fetched history rows.
  - Prefer `micro/small` first, then fallback to others, selecting top-N priority rows.

### Winner methodology (`sector_daily_winners`)
- Read current day priority rows (`is_priority=true`) ordered by `rank_in_sector`.
- Persist top-N with:
  - winner rank,
  - score breakdown,
  - issue flags,
  - source metadata.
- Enforced idempotency:
  - delete by (`sector_key`, `as_of_date`) before upsert.

### Quality gates and risk overlays
- **Prediction quality gate** in sector digest path checks:
  - successful symbol count threshold,
  - coverage ratio,
  - scored ratio,
  - top-5 next-week mean score,
  - top-vs-bottom spread adequacy.
- **Guardrails** applied before final digest:
  - cluster breadth guardrail,
  - macro guardrail,
  - event-risk guardrail.
- **Run-level data checks**:
  - low symbol coverage,
  - short history flags,
  - rollup symbol count mismatch warning.

---

## 7) Operational runbook (daily + weekly)

### Daily runbook (AI production)
1. **Preflight credentials**
   - Confirm valid Breeze session token.
   - Confirm `SUPABASE_URL` and `SUPABASE_KEY` are set.
2. **(If membership updates needed) Re-apply curation**
   - `python scripts/curate_ai_sector.py`
3. **Refresh ranking**
   - `python scripts/refresh_sector_priority_rankings.py --sector ai --top-n 10`
4. **Refresh winners**
   - `python scripts/refresh_ai_daily_winners.py`
5. **Run AI sector with curated priority universe**
   - `python main.py --sector ai --sector-priority-only --sector-priority-top-n 10`
6. **Post-run checks**
   - Validate winner rows for today in `sector_daily_winners`.
   - Check issue summaries in ranking metadata (`meta.issues`).
   - Confirm no stale symbols in same-day `symbol_daily_features` slice.

### Weekly runbook (stability and quality review)
1. Run pull-only health check:
   - `python scripts/validate_data_pull_only.py`
2. Run full multi-sector validation (controlled window):
   - `python scripts/validate_all_sectors_live.py`
3. Review `data/reports/live_sector_validation.json` for:
   - no-data skip spikes,
   - hard-failure clusters,
   - session-expiry interruptions.
4. Review AI allowlist relevance and adjust backups if needed.
5. Confirm SQL constraints/indexes remain intact for ranking/winner tables.

---

## 8) Automation blueprint to reuse for other sectors

### Reusable pattern
1. **Define sector universe policy**
   - Build strict include/exclude rules and override reason schema.
2. **Curate mappings**
   - Enforce sector membership in `instrument_sector_map` + `sector_overrides`.
   - Canonicalize one active exchange per symbol (NSE-first).
3. **Persist ranking artifacts**
   - Populate `sector_priority_rankings` daily for target sector.
4. **Persist winners**
   - Populate `sector_daily_winners` top-N with idempotent same-day refresh.
5. **Run sector in priority mode**
   - `main.py --sector <sector> --sector-priority-only --sector-priority-top-n <N>`
6. **Validate and monitor**
   - Pull-only coverage report + live digest validation + issue trend checks.

### Sector onboarding template

| Step | Deliverable |
|---|---|
| Universe curation | `<sector>_allowlist` policy + override upsert script |
| Data resilience | Confirm fetch fallback and issue coverage for sector symbols |
| Ranking activation | Daily ranking refresh script params finalized |
| Winner activation | Daily top-N persistence verified |
| Monitoring | Sector-specific coverage/error thresholds in weekly review |

---

## 9) Remaining risks and mitigations

| Risk | Impact | Mitigation already in place | Next hardening step |
|---|---|---|---|
| Breeze session expiry mid-run | Sector runs halt | Explicit actionable expiry error + preflight session creation | Add token freshness check + alert before batch jobs |
| High no-data ratio for specific sectors | Low coverage and weak confidence | Skip classification (`no_data_skipped`) and coverage stats | Add sector-level minimum-coverage auto-abort policy |
| External cap-source HTML/API changes | Ranking feature degradation | Multi-source fallback and source tagging in metadata | Add source health metrics and parser regression tests |
| Incorrect sector mappings reintroduced | Universe contamination | Strict override-driven curation flow | Add scheduled drift detector against allowlist |
| Same-day rerun data contamination | Misleading leaders/laggards | Delete-then-upsert for same-day feature slice | Add checksum-based post-write consistency checks |

---

## 10) Checklist for extending to top 5 sectors

### Execution checklist
- [ ] Choose top 5 sectors by business priority and data availability.
- [ ] Create strict allowlist + backup set for each sector (same policy shape as AI).
- [ ] Build `<sector>` curation scripts modeled on `scripts/curate_ai_sector.py`.
- [ ] Run curation and verify `instrument_sector_map` + `sector_overrides` consistency.
- [ ] Refresh rankings for each sector:
  - `python scripts/refresh_sector_priority_rankings.py --sector <sector> --top-n 10`
- [ ] Persist winners for each sector (extend AI winner script pattern to generic sector runner).
- [ ] Run priority-only sector execution:
  - `python main.py --sector <sector> --sector-priority-only --sector-priority-top-n 10`
- [ ] Validate with pull-only and live validation scripts; capture baseline coverage/failure metrics.
- [ ] Define per-sector guardrail thresholds (coverage floor, no-data ceiling, score spread floor).
- [ ] Publish weekly review dashboard/report from validation JSONs and Supabase tables.

### Suggested rollout order
1. Sector with best existing coverage and least no-data.
2. Sector with moderate complexity and stable mapping quality.
3. Remaining three sectors in two phased waves (2 + 1) after first-week metrics review.

---

## Appendix: tables/scripts touched in this fix cycle

- Tables: `sector_priority_rankings`, `sector_daily_winners`, `instrument_sector_map`, `sector_overrides`, `symbol_daily_features`, `sector_daily_rollup`, `sector_period_rollup`
- Scripts: `scripts/curate_ai_sector.py`, `scripts/refresh_sector_priority_rankings.py`, `scripts/refresh_ai_daily_winners.py`, `scripts/validate_all_sectors_live.py`, `scripts/validate_data_pull_only.py`
- Primary modules: `src/sector_priority.py`, `src/sector_registry.py`, `src/breeze_client.py`, `src/sector_audit.py`, `src/analysis_store.py`, `main.py`
