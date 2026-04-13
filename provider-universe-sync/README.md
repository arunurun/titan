# Provider Universe Sync

Standalone sync service (kept separate from Titan runtime code) that refreshes NSE+BSE instruments into Supabase.

## What this service updates
- `market_instruments`
- `instrument_sector_map`
- `sector_catalog`
- `scanner_runs`

## Data source
- ICICI scrip master (`StockScriptNew.csv`) for broad NSE/BSE coverage.
- Sector assignment strategy is hybrid:
  - Official provider sector when available.
  - Supabase `sector_overrides` takes precedence.
  - Fallback sector is `unknown`.

## Run locally
1. Create `.env` (or export env vars):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - Optional: `SCRIP_MASTER_URL`
2. Install deps: `pip install -r requirements.txt`
3. Run: `python -m src.main`

## GitHub Actions
- Weekly schedule: Sunday 07:00 IST (`30 1 * * 0` UTC).
- Manual trigger is enabled via `workflow_dispatch`.

## Separate repository usage
This folder is intentionally isolated. You can copy/push it as its own Git repository without Titan code.
