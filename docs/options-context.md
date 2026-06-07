# Options context (sector digests)

Titan sector digests can include **open-interest (OI) context** from NSE F&O option chains. This is **display-first** corroboration: it does not change equity technical scoring (`calculate_equity_technical_score` stays z-score + volume participation only).

## Phases

| Phase | Scope | Fetch cadence |
|-------|--------|----------------|
| 1 | Sector benchmark index (default **NIFTY**) | Once per sector run |
| 2 | F&O stocks in sector universe | Per symbol (allowlist only) |
| 3 | Signal engine Tier-2 corroborators | Uses Phase 2 audit fields |

## Data flow

1. `run_sector_live` prefetches NIFTY cash OHLC and fetches **one** index option chain (`sector_options_context` on each audit + digest header block `▸ Sector options context`).
2. `build_equity_live_audit` fetches a stock chain when the symbol is in `config/fno_symbols.yaml` (or the built-in Nifty-50 default list).
3. `signal_v2.layer_b` may add corroborators when options align with bearish tape:
   - **into call OI wall** — spot within ~1% of max call-OI strike + trim/bearish context
   - **below put OI support** — spot below max put-OI strike + distribution / negative tape

## Key fields

**Sector digest**

- `sector_pcr`, `sector_put_wall_strike`, `sector_call_wall_strike`, `sector_options_expiry`
- `sector_spot_vs_put_wall_pct`, `sector_spot_vs_call_wall_pct`

**Per-symbol audit (F&O only)**

- `pcr`, `put_oi_wall_strike`, `call_oi_wall_strike`, `options_expiry`
- `spot_vs_put_wall_pct`, `spot_vs_call_wall_pct`
- `option_chain_unavailable` — `False` when chain fetch succeeded

## Code paths

| Role | Module |
|------|--------|
| Breeze fetch + expiry fallback | `src/breeze_client.py` — `fetch_option_metrics_for_underlying`, `fetch_option_metrics_with_expiry_fallback` |
| PCR / walls | `src/titan_engine.py` — `get_pcr`, `find_call_put_oi_walls` |
| F&O allowlist + audit helpers | `src/options_context.py` |
| Email blocks | `src/sector_audit.py` — `_format_sector_options_context_block`, `_format_symbol_options_context_block` |
| Tier-2 corroborators | `src/signal_v2.py` — `_options_into_call_wall`, `_options_below_put_support` |

## Errors

Missing chains, zero OI, or API failures set `option_chain_unavailable=True` and skip corroborators. Per-stock fetch errors are logged per symbol; the sector run continues.
