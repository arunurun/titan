# Sector reconciliation status (Supabase `sector_catalog` vs Titan allowlists)

Generated from project tooling and manual Step 4 runs. **Unknown** is a routing bucket, not a tradable sector universe.

## Supabase active sectors (25)

From `scripts/list_sector_catalog.py` (`sector_catalog.is_active = true`):

`ai`, `auto`, `auto_ancillary`, `banks_private`, `banks_psu`, `capital_goods_industrials`, `cement_building_materials`, `chemicals`, `consumer_discretionary`, `defence`, `fmcg_staples`, `infrastructure_construction`, `insurance`, `it`, `logistics`, `media`, `metals_mining`, `nbfc_financial_services`, `oil_gas_energy`, `pharma_healthcare`, `power_utilities`, `realty_reits`, `telecom`, `textiles`, `unknown`

## Reconciled with strict JSON allowlist + curation script

These have `data/sector_allowlists/<sector_key>.json` and were processed with `scripts/curate_sector_strict.py` (NSE-first, non-allowlist → `unknown`, `sector_overrides` traceability):

| Batch | Sectors |
|-------|---------|
| AI + Step 4 round 1 (earlier) | `ai`, `defence`, `power_utilities`, `capital_goods_industrials`, `banks_psu`, `infrastructure_construction` |
| Step 4 round 2 (earlier) | `nbfc_financial_services`, `realty_reits`, `metals_mining`, `oil_gas_energy`, `auto_ancillary` |
| Step 4 round 3 (this session) | `auto`, `banks_private`, `cement_building_materials`, `chemicals`, `consumer_discretionary`, `fmcg_staples`, `insurance`, `it`, `logistics`, `media`, `pharma_healthcare`, `telecom`, `textiles` |

**Total equity sectors with strict allowlists: 23** (all active keys except **`unknown`**).

## Not reconcile-cleaned (by design)

- **`unknown`**: sink sector for excluded symbols; no product allowlist.

## Operations: ranking + winners + Titan

For any sector after curation:

```powershell
$env:BREEZE_SESSION_TOKEN = (python scripts/fetch_breeze_session_from_supabase.py)
$env:BREEZE_HIST_CALL_INTERVAL_SECONDS = "0.4"
python scripts/refresh_sector_daily_winners.py --sector <sector_key> --top-n 10
python main.py --sector <sector_key> --sector-priority-only --sector-priority-top-n 10 --sector-workers 1
```

Round 3 sectors may still need ranking refresh + Titan after you run the above with a valid Breeze session.

## Notes

- **Overlapping symbols**: the same NSE symbol may appear in multiple sector JSON files (e.g. utilities vs oil & gas). `sector_overrides` is unique on `(exchange, symbol)`; last curation wins. Prefer **non-overlapping** allowlists or document ownership.
- **Insurance** allowlist has **10** liquid listings (smaller universe than 15).
- **Auto**: `TATAMOTORS` was missing from `market_instruments`; allowlist uses **`TATAMTRDVR`** (DVR) for Tata Motors exposure where the ordinary line is absent in DB.
