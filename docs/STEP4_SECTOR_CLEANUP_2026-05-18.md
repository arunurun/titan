# Step 4 — Multi-sector strict cleanup (2026-05-18)

## Why parallel “agents” failed earlier (API limit)

Cursor’s **Task / subagent** feature calls the model on a **separate usage pool** from the main chat. When that pool is exhausted, resumes return **“API usage limit reached”**.

- **Cause:** Subagent quota for your Cursor plan / billing period was temporarily used up (parallel agents consume it quickly).
- **When it refreshes:** Cursor does not expose an exact clock in the repo. In practice limits usually reset on a **daily (UTC) cycle** or at your **billing period** boundary, depending on product tier. Check **Cursor Settings → Account / Usage** (or the in-app usage indicator) for the authoritative reset time.

**Workaround used here:** run the same workflow as a **single agent** in the main session (this document).

---

## Goal (Step 4)

Apply the **same strict pattern as AI** to the next five sectors:

1. `defence`
2. `power_utilities`
3. `capital_goods_industrials`
4. `banks_psu`
5. `infrastructure_construction`

Pattern: **JSON allowlist → Supabase `instrument_sector_map` curation (NSE-first) → `sector_overrides` traceability → refresh rankings + daily winners → Titan `--sector-priority-only` validation.**

---

## What was added in the repo

| Path | Purpose |
|------|---------|
| `data/sector_allowlists/<sector_key>.json` | Declared strict symbol set + `policy` id |
| `scripts/curate_sector_strict.py` | Applies allowlist to Supabase (mirrors `scripts/curate_ai_sector.py`) |
| `scripts/refresh_sector_daily_winners.py` | Generic `build_sector_rankings` + `persist_sector_rankings` + `persist_daily_winners` |
| `scripts/fetch_breeze_session_from_supabase.py` | Prints `session_config` token for local `$env:BREEZE_SESSION_TOKEN` |
| `main.py` | Loads `.env` with **`override=False`** so injected Breeze token is not overwritten |

---

## Curation run summary (executed against Supabase)

Commands used:

```bash
python scripts/curate_sector_strict.py --sector-key defence
python scripts/curate_sector_strict.py --sector-key power_utilities
python scripts/curate_sector_strict.py --sector-key capital_goods_industrials
python scripts/curate_sector_strict.py --sector-key banks_psu
python scripts/curate_sector_strict.py --sector-key infrastructure_construction
```

### `defence`

- **Allowlist size:** 15 declared → **15 resolved** on NSE (`allowlist_unresolved_symbols`: none).
- **Removed from sector:** 36 unique symbols (plus duplicate-exchange clean-up); large prior universe trimmed to core defence names.
- **Active curated pairs:** 15 NSE rows (see script JSON: `allowlist_active_pairs`).

### `power_utilities`

- **Final allowlist:** 15 declared → **15 resolved** (after ticker fix: `GSPL` substituted where `GUJGAS` / `GUJGASLT` were not present in `market_instruments`).
- **Removed from sector:** ~287 instruments in first pass (from a very noisy prior universe).

### `capital_goods_industrials`

- **Final allowlist:** 15 declared → **15 resolved** (`ELGIEQUIP`, `SKFINDIA` used where raw `ELGI` / `JYOTICNC` were not in DB).
- **Removed from sector:** ~279 instruments in first pass.

### `banks_psu`

- **Allowlist:** 13 declared → **12 resolved**; **`JKBANK` unresolved** (not found as active `market_instruments` row at run time — add correct listing symbol to DB or adjust allowlist).
- **Note:** `instrument_rows_deactivated: 0` indicates the sector was already tight or matched allowlist after prior state.

### `infrastructure_construction`

- **Final allowlist:** 15 declared → **15 resolved** (`GMRAIRPORT` used instead of `GMRINFRA`).
- **Removed from sector:** ~371 instruments in first pass.

---

## Rankings + daily winners + Titan validation (completed 2026-05-18)

### Token loading (local)

- **`main.py` previously used `load_dotenv(..., override=True)`**, which **overwrote** a fresh `BREEZE_SESSION_TOKEN` set in the shell with a **stale** value from `.env`. This is fixed: `main.py` now uses **`override=False`** so **OS env wins** (e.g. token injected from Supabase before the run).
- **`scripts/fetch_breeze_session_from_supabase.py`** prints the current token from `session_config(id=1)` for local use.

**PowerShell pattern (run before ranking refresh or Titan):**

```powershell
$env:BREEZE_SESSION_TOKEN = (python scripts/fetch_breeze_session_from_supabase.py)
$env:BREEZE_HIST_CALL_INTERVAL_SECONDS = "0.4"
```

### Ranking + `sector_daily_winners` refresh

All five sectors completed **`scripts/refresh_sector_daily_winners.py --top-n 10`** with `rank_persist.persisted: true` and `winner_persist.persisted: true`.

**Top-10 winner symbols (persisted, snapshot):**

| Sector | Winners (NSE, order) |
|--------|------------------------|
| `defence` | IDEAFORGE, MTARTECH, DATAPATTNS, MIDHANI, ASTRAMICRO, BEL, ZENTEC, BEML, HAL, DYNAMATECH |
| `power_utilities` | GSPL, ATGL, SJVN, ADANIGREEN, ADANIPOWER, IGL, PETRONET, CESC, NTPC, MGL |
| `capital_goods_industrials` | KIRLOSENG, SKFINDIA, BHEL, GREAVESCOT, VOLTAS, WABAG, ELECON, TIMKEN, TRF, ELGIEQUIP |
| `banks_psu` | BANKINDIA, BANKBARODA, MAHABANK, PSB, CANBK, IOB, PNB, CENTRALBK, UCOBANK, UNIONBANK |
| `infrastructure_construction` | KPIL, ENGINERSIN, KNRCON, PNCINFRA, GRINFRA, HGINFRA, KEC, RITES, LT, IRCON |

*(Ranking pool: 15 symbols except `banks_psu` where allowlist resolves to **12** names; top-10 still fills.)*

### Titan priority-only digest

**Command used for each sector:**

```bash
python main.py --sector <sector> --sector-priority-only --sector-priority-top-n 10 --sector-workers 1
```

**Result:** **10/10 succeeded** for all five sectors (digest mode: 1 Gemini call each).

Per-run logs: `data/reports/step4_2026-05-18/titan_<sector>.log`

**Non-fatal:** LLM digest memory table missing (`llm_digest_memory` 404) — digest still generated and audit email sent.

---

## Allowlist maintenance

- Edit JSON under `data/sector_allowlists/`.
- Re-run `python scripts/curate_sector_strict.py --sector-key <key>`.
- **Important:** `sector_overrides` uniqueness is `(exchange, symbol)` only (see `sql/create_sector_registry_tables.sql`). Overrides for one symbol can collide across sectors; keep allowlists **non-overlapping** where possible.

---

## Follow-ups

1. Resolve **`JKBANK`** for `banks_psu` if that name must appear in the DB-backed allowlist (add correct `market_instruments` row or adjust ticker).
2. Optionally extend `.github/workflows/run_titan_now.yml` with the same **priority-only branch** used for `ai` for these sector keys (same CLI pattern).
3. Optionally create `public.llm_digest_memory` in Supabase to remove digest-memory 404 warnings.
