# Step 4 — Round 2 (next top 5 sectors) — 2026-05-18

## Sectors (priority ranks 7–11 from `docs/NSE_SECTOR_PRIORITY_RECONCILIATION_2026-05-18.md`)

1. `nbfc_financial_services`
2. `realty_reits`
3. `metals_mining`
4. `oil_gas_energy`
5. `auto_ancillary`

## Artifacts

| Path | Purpose |
|------|---------|
| `data/sector_allowlists/<sector>.json` | Strict 15-name allowlist per sector |
| `data/reports/step4_round2_2026-05-18/titan_<sector>.log` | Full Titan digest run log |
| `scripts/curate_sector_strict.py` | Chunked Supabase writes (fixes NBFC-scale sector `IN` / upsert limits) |

## Curation notes

- **NBFC:** First run failed with PostgREST `400 Bad Request` on a very large `.in_("instrument_id", …)` — fixed by **batching** updates and upserts in `curate_sector_strict.py`. Sector had ~990 instruments to move to `unknown`.
- **NBFC ticker:** `UGRO` → **`UGROCAP`** (resolved in `market_instruments`).
- **Oil & gas:** `HPCL` → **`HINDPETRO`** (NSE listing symbol). Re-ran curation to refresh include overrides to **15/15** resolved.

## Ranking + daily winners (top 10)

| Sector | Top-10 winners (persisted, order) |
|--------|-----------------------------------|
| `nbfc_financial_services` | MUTHOOTFIN, MANAPPURAM, PNBHOUSING, BAJAJFINSV, AAVAS, CANFINHOME, FIVESTAR, LICHSGFIN, UGROCAP, SBFC |
| `realty_reits` | SOBHA, NESCO, SIGNATURE, MAHLIFE, SUNTECK, SWANCORP, PHOENIXLTD, BRIGADE, ANANTRAJ, PARSVNATH |
| `metals_mining` | WELCORP, JSWSTEEL, HINDALCO, SAIL, NMDC, HINDZINC, TATASTEEL, RATNAMANI, JINDALSTEL, HINDCOPPER |
| `oil_gas_energy` | GSPL, ATGL, OIL, ONGC, GAIL, IGL, PETRONET, CASTROLIND, CHENNPETRO, MGL |
| `auto_ancillary` | APOLLOTYRE, ENDURANCE, MOTHERSON, BOSCHLTD, EXIDEIND, SONACOMS, AMARAJABAT, CEATLTD, JKTYRE, TIMKEN |

Persist verified: `persist_sector_rankings` and `persist_daily_winners` returned `persisted: true`.

## Titan validation

Command pattern (same as round 1):

```powershell
$env:BREEZE_SESSION_TOKEN = (python scripts/fetch_breeze_session_from_supabase.py)
$env:BREEZE_HIST_CALL_INTERVAL_SECONDS = "0.4"
python main.py --sector <sector> --sector-priority-only --sector-priority-top-n 10 --sector-workers 1
```

**Outcome:** **10/10 succeeded** for all five sectors (digest mode, 1 Gemini call each).

## Cross-sector symbol overlap

Some names (e.g. **GSPL**, **IGL**, **MGL**, **ATGL**, **PETRONET**) appear in more than one sector allowlist. `sector_overrides` is unique on `(exchange, symbol)` only — the **last** curation that touched a symbol wins for that table. If you need mutually exclusive sector ownership for overlapping tickers, split allowlists or use a dedicated override policy per symbol.

## Rerun recipe

```powershell
cd <repo>
$env:BREEZE_SESSION_TOKEN = (python scripts/fetch_breeze_session_from_supabase.py)
$env:BREEZE_HIST_CALL_INTERVAL_SECONDS = "0.4"

foreach ($s in "nbfc_financial_services","realty_reits","metals_mining","oil_gas_energy","auto_ancillary") {
  python scripts/curate_sector_strict.py --sector-key $s
  python scripts/refresh_sector_daily_winners.py --sector $s --top-n 10
  python main.py --sector $s --sector-priority-only --sector-priority-top-n 10 --sector-workers 1
}
```
