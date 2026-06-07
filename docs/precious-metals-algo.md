# Precious Metals Allocator

DXY × GSR-band matrix allocator for gold/silver/cash with an SGE physical-demand overlay. Used in `metals_mining` sector digest emails when `TITAN_PM_MACRO_EMAIL=1` (default for that sector).

## Overview

`PreciousMetalsAlgo` in `src/precious_metals_algo.py`:

1. **DXY** sets total portfolio metals exposure (85% / 40% / 20% for weak / neutral / strong dollar).
2. **GSR band** (absolute gold/silver ratio, 50–60 band) sets gold vs silver tilt within the metals slice.
3. **SGE** confirms or trims total metals exposure (×1.15 tight, ×0.75 weak).

Z-scores use a 252-day rolling window by default (±1.0 thresholds). Tests use shorter windows.

## Inputs

| Column | Required | Description |
|--------|----------|-------------|
| `GOLD` | Yes | London/COMEX gold proxy (close) |
| `SILVER` | Yes | London/COMEX silver proxy (close) |
| `DXY` | Yes | US Dollar Index |
| `SGE_PREMIUM_PCT` | Optional | Shanghai premium vs London (%) |
| `SGE_WITHDRAWAL` | Optional | SGE withdrawal volume; z-score adds tightness signal |
| `SGE_GOLD` | Optional | Shanghai gold price; used to derive premium vs `GOLD` |

Sector emails read daily series from `data/cache/pm_macro_series.csv` (see example file). Override path with `TITAN_PM_MACRO_CSV`.

## Logic

### DXY (z-score, threshold ±1.0)

| State | Condition | Total metals |
|-------|-----------|--------------|
| WEAK | z ≤ −1 | 85% |
| NEUTRAL | else | 40% |
| STRONG | z ≥ +1 | 20% |

### GSR band (absolute `gsr_last = GOLD / SILVER`)

| Band | Condition |
|------|-----------|
| below | &lt; 50 |
| in | 50–60 |
| above | &gt; 60 |

### Within-metals tilt (gold / silver share of metals)

| DXY | GSR below | GSR in | GSR above |
|-----|-----------|--------|-----------|
| WEAK | 70/30 | 50/50 | 20/80 |
| NEUTRAL | 55/45 | 50/50 | 35/65 |
| STRONG | 60/40 | 55/45 | 40/60 |

### SGE multiplier (on total metals exposure)

| State | Condition | Multiplier |
|-------|-----------|------------|
| TIGHT | premium z ≥ +1 or withdrawal z ≥ +1 | ×1.15 (cap 100%) |
| WEAK | premium z ≤ −1 | ×0.75 |
| NEUTRAL | else | ×1.0 |

**STRONG DXY + SGE TIGHT:** enforce portfolio floors gold ≥ 20%, silver ≥ 10%.

### Conviction

- **HIGH** — aligned silver catch-up or gold-defensive setup (e.g. weak DXY + GSR above + SGE tight).
- **MIXED** — conflicting signals (e.g. strong DXY + SGE tight).
- **LOW** — strong DXY + weak SGE, or neutral with no edge.
- **MODERATE** — partial alignment.

## Email section

When enabled, `run_sector_live("metals_mining", digest=True)` appends `--- Precious metals macro ---` after the decision-first block. Formatter: `format_precious_metals_digest_lines()`.

Environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TITAN_PM_MACRO_EMAIL` | `1` for `metals_mining` | Enable/disable PM block |
| `TITAN_PM_MACRO_CSV` | `data/cache/pm_macro_series.csv` | Series cache path |
| `TITAN_PM_BOOK_INR` | `10000000` (₹100L) | Book size for USD allocation lines |

If CSV is missing, the section shows `Data unavailable — configure data/cache/pm_macro_series.csv` without failing the sector run.

## Usage

```python
from precious_metals_algo import (
    PreciousMetalsAlgo,
    format_precious_metals_digest_lines,
    load_pm_macro_series_from_csv,
)

data = load_pm_macro_series_from_csv()
algo = PreciousMetalsAlgo(z_window=252, z_threshold=1.0, sge_z_threshold=1.0)
features = algo.generate_features(data)
result = algo.execute_allocation_logic(features)
lines = format_precious_metals_digest_lines(result, features, "2026-06-05", book_value_inr=10_000_000)
```

## Populate `pm_macro_series.csv`

Copy `data/cache/pm_macro_series.csv.example` to `data/cache/pm_macro_series.csv` and refresh daily rows:

```text
date,GOLD,SILVER,DXY,SGE_PREMIUM_PCT,SGE_WITHDRAWAL
2026-06-05,2017.0,25.7,93.8,2.1,185.0
```

- **date** — ISO date (EOD as-of)
- **GOLD / SILVER / DXY** — aligned daily closes (≥252 rows recommended for production z-scores)
- **SGE_PREMIUM_PCT** — Shanghai vs London premium (%)
- **SGE_WITHDRAWAL** — warehouse withdrawal index or volume

Need at least ~20 overlapping rows for test windows; 252+ for live z-scores.

## Tests

```bash
pytest tests/test_precious_metals_algo.py tests/test_precious_metals_email_digest.py -q
pytest tests/test_sector_audit.py -k pm_macro -q
```
