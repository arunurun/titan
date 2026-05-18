# Institutional flow (FII / DII) — implementation roadmap

Titan’s **volume participation ratio (VPR)** is cash-volume vs its own recent average. It is **not** FII/DII net buy/sell and not delivery-based “absorption.”

## Next implementation step (when you pick a data source)

1. **Schema** — Add columns or a small table, for example:
   - `fii_net_crs` (optional `fii_buy_crs`, `fii_sell_crs`)
   - `dii_net_crs` (optional splits)
   - `as_of` (session date; align with Titan’s trade_date convention)
   - `symbol`, `exchange` (match `market_instruments` / audit keys)

2. **Ingest** — A scheduled or manual script loads your chosen feed (NSE/BSE provisional, CMOTS, vendor API), normalizes symbols, and upserts into that table.

3. **Audit wiring** — In `build_equity_live_audit` (`src/sector_audit.py`):
   - Set `institutional_flow["available"] = True` when a row exists for `(symbol, exchange, as_of)`.
   - Populate `institutional_flow` with `source`, `fii_net_crs`, `dii_net_crs`, `as_of`, etc.
   - **Fold into a separate score block** (e.g. `institutional_score` / `institutional_reasons`) so it is **never mixed** with VPR or `equity_technical_score`.

4. **Digest / email** — Optionally surface institutional context next to VPR in formatted lines once data is reliable.

Until then, audits carry `institutional_flow.available == False` and the inline `note` in the audit payload describes the gap.
