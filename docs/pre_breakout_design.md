# Titan Breakout Design — Breeze Stock Code + PRE_BREAKOUT Tier

Approved design reference for `feature/breakout-setup`. See `docs/breakout_logic.md` for live scanner behavior.

## Executive summary

| Track | Implementation |
|-------|----------------|
| **A — Breeze code** | Nullable `breeze_stock_code`; bulk resolution via scrip master + Supabase `market_instruments` overlay when non-null |
| **B — PRE_BREAKOUT** | `signal_tier = PRE_BREAKOUT` (display **SETUP**); parallel `evaluate_setup_as_of()`; no trade levels until trigger |

## Part A — Breeze stock code

- **Resolution:** once per scan via `build_breeze_code_map()` in `src/breakout_breeze_codes.py`
- **Primary:** `resolve_breeze_stock_code(sym, "NSE")`
- **Overlay:** `market_instruments.breeze_stock_code` when populated
- **Fallback:** uppercased NSE symbol (same as `breeze_client`)
- **Surfaces:** Supabase persist, proto field 39, report/email, `serialize_candidate`

## Part B — PRE_BREAKOUT (SETUP)

### Architecture

```
evaluate_bars_as_of()   → PASS | WATCH | FAIL
evaluate_setup_as_of()  → PRE_BREAKOUT | null
```

PASS/WATCH wins over PRE_BREAKOUT (no duplicate rows). `passed=true` only for PASS.

### Hard gates

| Gate | Smallcap | Microcap |
|------|----------|----------|
| Bars | ≥ 50 | ≥ 50 |
| Min price | ≥ ₹15 | ≥ ₹10 |
| Liquidity | ≥ ₹2 cr | ≥ ₹3 cr |
| Not parabolic | stage ≠ 3 | stage ≠ 3 |

### Setup-specific gates

| Check | Threshold |
|-------|-----------|
| Daily change | −2.0% to +2.9% |
| Volume mult | 1.5–2.5× (small), 1.5–2.3× (micro) |
| Vol persistence | ≥ 1 @ 1.5× |
| Base score | ≥ 58 |
| Consolidation days | ≥ 18 |
| Pivot proximity | ≥ 85 |
| 52w proximity | close ≥ 92% of 52w high |
| Stretch | < 2.5 ATR above SMA50 |
| Trend | close ≥ SMA50 OR (≥ SMA20 and vol ≥ 1.5×) |
| RSI | 45–68 |
| ADX | 18–32 |

Micro participation: **penalty-only** v1 (−15 setup rank points when decisively false).

### Output

- Report section: **Setup Watchlist** per tier
- Email: amber SETUP section
- Cap: top **10** per universe tier by `setup_rank`
- Trigger: close > pivot with tier vol and ≥ +3% day

### Backtest

`setup_to_breakout_rate` in `src/breakout_backtest.py` — precision@5/10/15 sessions.
CLI: `python scripts/breakout_backtest.py --setup-backtest`

## Schema

| Column | Type |
|--------|------|
| `breeze_stock_code` | text |
| `setup_trigger_price` | double precision |
| `setup_rank` | double precision |
