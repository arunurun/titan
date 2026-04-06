# Titan — sector-wise analysis (plan)

**Branch:** `sector` (merged to `main` when ready).  
**Tag reference:** `WorkingNSE6thApril2026`

---

## Goal

One entry point accepts a **sector id** (e.g. `defence`), resolves the **symbol + exchange** list for that sector, runs a **cash-equity style audit** per symbol (Breeze + metrics), optionally produces **Gemini narrative** within quota, and persists results.

**Done (v1):** single-sector path, in-sector parallelism, digest narrative, Supabase + email.  
**Later:** multiple sectors in one invocation with bounded workers.

---

## Implementation status (living checklist)

| # | Task | Status | Notes |
|---|------|--------|--------|
| 1 | **Sector registry** | **Done** | `data/sectors/<id>.csv` with `symbol`, `exchange` (NSE/BSE). `src/sector_registry.py`: `load_sector_instruments`, `MAX_SYMBOLS` cap. |
| 2 | **Resolver API** | **Done** | `load_sector_instruments(sector_id)` returns validated `SectorInstrument` list; unknown/empty errors. |
| 3 | **Abstract audit from NIFTY** | **Partial** | `src/sector_audit.py` — `build_equity_live_audit` for equities (z-score, absorption, intent); **no** per-stock option chain (flagged `option_chain_unavailable`). NIFTY remains `main.py --live`. |
| 4 | **Breeze layer** | **Done** | `fetch_equity_data`, scrip resolution via `breeze_scrip_master` / `StockScriptNew.csv` cache where needed. |
| 5 | **Orchestrator** | **Done** | `run_sector_live` in `sector_audit.py` — `ThreadPoolExecutor`, configurable workers. |
| 6 | **CLI** | **Done** | `python main.py --sector <id> [--sector-workers N] [--sector-max-symbols N] [--sector-digest]`. |
| 7 | **Persistence & email** | **Done** | One Supabase row per successful symbol; **digest email** for full run (combined text). **`--sector-digest`:** one Gemini call for sector narrative + per-line metrics; avoids free-tier per-symbol quota blowups. |
| 8 | **Tests** | **Done** | Resolver, sector audit (mocked Breeze/Gemini), CLI wiring; no live API in CI. |
| 9 | **Multi-sector parallel** | **Not started** | e.g. `run_sectors_parallel([...])` — after single-sector is stable in production. |

---

## Gemini & quota (locked behaviour)

- **Free tier:** prefer **`--sector-digest`** (one `generate_content` per sector run; compliance retry may add a second call).
- **Key rotation:** `GEMINI_API_KEY`, optional `GEMINI_API_KEY_2`…`GEMINI_API_KEY_5`, or `GEMINI_API_KEYS` (comma-separated). On **429 / quota**, `brain.py` tries the next key before long backoff on the last key.
- **CI:** branch `sector` workflow runs `python main.py --sector defence --sector-workers 4 --sector-digest`; optional GitHub secret `GEMINI_API_KEY_2`.

---

## Design choices (v1)

- **Stock list:** static CSV per sector (manual maintenance).
- **Concurrency:** default modest **max_workers** (e.g. 4); each worker uses its own Breeze session where applicable.
- **GitHub Actions:** **`--sector-digest`** + **`--sector-max-symbols`** available if limits need tightening further.

---

## References (repo)

- `data/sectors/defence.csv` — universe list.
- `src/sector_registry.py`, `src/sector_audit.py`, `src/breeze_scrip_master.py`, `src/breeze_client.py`.
- `src/brain.py` — narratives, rotation, `_sector` digest payload.
- `main.py` — `--sector` flags.
- `.github/workflows/market_audit.yml` — NIFTY `--live` vs sector `--sector` by branch.
- `config/.env.example` — Gemini multi-key env vars documented.
