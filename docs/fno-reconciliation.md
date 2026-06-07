# F&O Reconciliation — Sector Allowlists vs NSE fo_mktlots

Generated: 2026-06-07 | Branch: `options`

## Sources

- **NSE F&O truth:** [fo_mktlots.csv](https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv) (212 stock underlyings; cached at `data/cache/fo_mktlots.csv` for tests)
- **Titan allowlist:** `config/fno_symbols.yaml` (148 symbols)
- **Breeze NFO codes:** `config/fno_breeze_mapping.yaml` (148 entries)
- **Sector universes:** `data/sector_allowlists/*.json` (23 sectors, 326 unique symbols)

## Summary

| Metric | Count |
|--------|------:|
| Total unique sector symbols | 326 |
| NSE F&O stock underlyings (fo_mktlots) | 212 |
| Sector symbols confirmed NSE F&O | 142 |
| Sector symbols **not** in NSE F&O | 184 |
| Sector F&O symbols in fno_symbols.yaml | 142 |
| fno_symbols.yaml entries (total) | 148 |
| fno_breeze_mapping.yaml entries | 148 |
| YAML entries not in any sector | 6 |

### Allowlist accuracy (validated 2026-06-07)

- **Incorrectly marked F&O (in YAML, not in NSE):** 0 — none
- **Missing from YAML (sector + NSE F&O):** 0 — none
- **Non-F&O sector symbols in mapping file:** 0 — none
- **YAML extras (not in any sector, index constituents):** ADANIENT, ADANIPORTS, APOLLOHOSP, ASIANPAINT, COALINDIA, GRASIM

No corrections to `fno_symbols.yaml` were required.

### Sample Breeze NFO mappings

| NSE symbol | Breeze NFO code |
|------------|-----------------|
| BEL | BHAELE |
| INDUSTOWER | BHAINF |
| HAL | HINAER |
| BHARTIARTL | BHAAIR |

## Mapping maintenance

When NSE adds/removes F&O underlyings or ICICI changes scrip codes:

1. Refresh ICICI scrip cache (automatic on next Breeze fetch, or copy `StockScriptNew.csv` to `data/cache/`).
2. Run `python scripts/build_fno_breeze_mapping.py` — downloads latest `fo_mktlots.csv`, cross-checks `fno_symbols.yaml`, writes `config/fno_breeze_mapping.yaml`.
3. Update `config/fno_symbols.yaml` if sector allowlists or NSE membership changed.
4. Run `pytest tests/test_fno_breeze_mapping.py` (mapping invariants; no credentials required).
5. Optionally run `pytest -m breeze_live` locally with `BREEZE_API_KEY`, `BREEZE_SECRET`, `BREEZE_SESSION_TOKEN` to spot-check live chains.

**Note:** Correct mappings do not guarantee zero Breeze API failures — HTTP 500 / “Error while calling service” can be transient. Tests validate mapping completeness and mocked API contract; live tests are skipped in CI without credentials.

### Cross-sector duplicates (8 symbols)

| Symbol | F&O status | Sectors |
|--------|-----------|---------|
| LT | F&O | capital_goods_industrials, infrastructure_construction |
| PAGEIND | F&O | consumer_discretionary, textiles |
| PETRONET | F&O | oil_gas_energy, power_utilities |
| ATGL | non-F&O | oil_gas_energy, power_utilities |
| GSPL | non-F&O | oil_gas_energy, power_utilities |
| IGL | non-F&O | oil_gas_energy, power_utilities |
| MGL | non-F&O | oil_gas_energy, power_utilities |
| TIMKEN | non-F&O | auto_ancillary, capital_goods_industrials |

## Recent workflow fetch status (optional)

| Run | Sector | F&O symbol | Fetch result |
|-----|--------|------------|--------------|
| [27093662261](https://github.com/arunurun/titan/actions/runs/27093662261) | defence | HAL, BEL, BDL | Chain fetch **attempted**, all failed (Breeze HTTP 500) |
| [27093662261](https://github.com/arunurun/titan/actions/runs/27093662261) | defence | MAZDOCK, COCHINSHIP | Not in priority run (no fetch log) |
| [27093703747](https://github.com/arunurun/titan/actions/runs/27093703747) | telecom | INDUSTOWER | Chain fetch **attempted**, failed (Breeze HTTP 500) |
| [27093703747](https://github.com/arunurun/titan/actions/runs/27093703747) | telecom | BHARTIARTL, IDEA | Not in priority subset (5/15 symbols run) |

Note: Runs used sector-priority mode (top-N), not full allowlist. Non-F&O symbols correctly show `not in F&O universe (display only)`.

## Per-sector breakdown

### auto (15)
- **With F&O (10):** MARUTI, M&M, EICHERMOT, BAJAJ-AUTO, HEROMOTOCO, TVSMOTOR, ASHOKLEY, FORCEMOT, TIINDIA, HYUNDAI
- **Without F&O (5):** TATAMTRDVR, OLECTRA, ESCORTS, VSTTILLERS, MAHSCOOTER

### auto_ancillary (15)
- **With F&O (6):** BHARATFORG, MOTHERSON, SONACOMS, UNOMINDA, BOSCHLTD, EXIDEIND
- **Without F&O (9):** ENDURANCE, RKFORGE, SCHAEFFLER, TIMKEN, AMARAJABAT, APOLLOTYRE, CEATLTD, JKTYRE, MRF

### banks_private (15)
- **With F&O (10):** HDFCBANK, ICICIBANK, KOTAKBANK, AXISBANK, INDUSINDBK, IDFCFIRSTB, FEDERALBNK, BANDHANBNK, YESBANK, RBLBANK
- **Without F&O (5):** CSBBANK, CUB, KARURVYSYA, TMB, SOUTHBANK

### banks_psu (13)
- **With F&O (7):** SBIN, BANKBARODA, PNB, CANBK, UNIONBANK, INDIANB, BANKINDIA
- **Without F&O (6):** CENTRALBK, UCOBANK, IOB, MAHABANK, PSB, JKBANK

### capital_goods_industrials (15)
- **With F&O (5):** LT, SIEMENS, ABB, BHEL, VOLTAS
- **Without F&O (10):** THERMAX, KIRLOSENG, GREAVESCOT, SKFINDIA, ELECON, ELGIEQUIP, TIMKEN, HONAUT, TRF, WABAG

### cement_building_materials (15)
- **With F&O (4):** ULTRACEMCO, SHREECEM, AMBUJACEM, DALBHARAT
- **Without F&O (11):** ACC, RAMCOCEM, JKCEMENT, ORIENTCEM, HEIDELBERG, PRSMJOHNSN, NCLIND, SANGHIIND, DECCANCE, BIRLACORPN, INDIACEM

### chemicals (15)
- **With F&O (4):** UPL, PIIND, PIDILITIND, SRF
- **Without F&O (11):** TATACHEM, AARTIIND, DEEPAKNTR, VINATIORGA, ALKYLAMINE, NOCIL, GHCL, NAVINFLUOR, BALAMINES, FINEORG, LXCHEM

### consumer_discretionary (15)
- **With F&O (5):** TRENT, TITAN, INDIGO, JUBLFOOD, PAGEIND
- **Without F&O (10):** DEVYANI, WESTLIFE, BATAINDIA, RELAXO, VIPIND, DOMS, ZOMATO, BECTORFOOD, CERA, KAJARIACER

### defence (15)
- **With F&O (5):** HAL, BEL, BDL, MAZDOCK, COCHINSHIP
- **Without F&O (10):** GRSE, DATAPATTNS, PARAS, ASTRAMICRO, MTARTECH, DYNAMATECH, MIDHANI, BEML, IDEAFORGE, ZENTEC

### fmcg_staples (15)
- **With F&O (11):** HINDUNILVR, ITC, NESTLEIND, BRITANNIA, DABUR, MARICO, COLPAL, GODREJCP, VBL, TATACONSUM, RADICO
- **Without F&O (4):** EMAMILTD, UNITEDSPI, PGHH, BLISSGVS

### infrastructure_construction (15)
- **With F&O (3):** LT, RVNL, GMRAIRPORT
- **Without F&O (12):** NCC, IRCON, RITES, HGINFRA, PNCINFRA, KNRCON, GRINFRA, KEC, KPIL, JWL, IRB, ENGINERSIN

### insurance (10)
- **With F&O (6):** LICI, ICICIGI, SBILIFE, HDFCLIFE, MAXHEALTH, ICICIPRULI
- **Without F&O (4):** STARHEALTH, NEWINDIA, GICRE, NIACL

### it (15)
- **With F&O (9):** TCS, INFY, WIPRO, HCLTECH, TECHM, MPHASIS, COFORGE, PERSISTENT, OFSS
- **Without F&O (6):** LTTS, LTIM, SONATSOFTW, ZENSART, NEWGEN, BSOFT

### logistics (11)
- **With F&O (2):** CONCOR, DELHIVERY
- **Without F&O (9):** MAHLOG, TCIEXP, NAVKARCORP, ALLCARGO, BLUEDART, VRLLOG, AEGISLOG, RITCO, SNOWMAN

### media (15)
- **With F&O (1):** NAM-INDIA
- **Without F&O (14):** SUNTV, ZEEL, NETWORK18, DISHTV, BALAJITELE, SAREGAMA, NDTV, HTMEDIA, JAGRAN, PVRINOX, INOXLEISUR, DEN, TIPS, TCNSBRANDS

### metals_mining (15)
- **With F&O (10):** TATASTEEL, JSWSTEEL, HINDALCO, VEDL, NATIONALUM, HINDZINC, SAIL, JINDALSTEL, NMDC, APLAPOLLO
- **Without F&O (5):** MOIL, RATNAMANI, WELCORP, HINDCOPPER, JSL

### nbfc_financial_services (15)
- **With F&O (10):** BAJFINANCE, BAJAJFINSV, CHOLAFIN, SHRIRAMFIN, LTF, MUTHOOTFIN, MANAPPURAM, PNBHOUSING, LICHSGFIN, ANGELONE
- **Without F&O (5):** CANFINHOME, AAVAS, UGROCAP, FIVESTAR, SBFC

### oil_gas_energy (15)
- **With F&O (8):** RELIANCE, ONGC, OIL, BPCL, HINDPETRO, IOC, GAIL, PETRONET
- **Without F&O (7):** MRPL, CHENNPETRO, ATGL, GSPL, CASTROLIND, IGL, MGL

### pharma_healthcare (15)
- **With F&O (11):** SUNPHARMA, DRREDDY, CIPLA, LUPIN, BIOCON, DIVISLAB, AUROPHARMA, ALKEM, TORNTPHARM, LAURUSLABS, ZYDUSLIFE
- **Without F&O (4):** IPCALAB, SYNGENE, GRANULES, NATCOPHARM

### power_utilities (15)
- **With F&O (8):** NTPC, POWERGRID, TATAPOWER, ADANIGREEN, ADANIPOWER, JSWENERGY, NHPC, PETRONET
- **Without F&O (7):** TORNTPOWER, SJVN, CESC, ATGL, IGL, MGL, GSPL

### realty_reits (15)
- **With F&O (6):** DLF, GODREJPROP, OBEROIRLTY, PRESTIGE, PHOENIXLTD, LODHA
- **Without F&O (9):** BRIGADE, SOBHA, MAHLIFE, ANANTRAJ, SIGNATURE, SWANCORP, SUNTECK, PARSVNATH, NESCO

### telecom (15)
- **With F&O (3):** BHARTIARTL, IDEA, INDUSTOWER
- **Without F&O (12):** TATACOMM, ONMOBILE, RAILTEL, MTNL, HFCL, ITI, NELCO, ROUTE, TEJASNET, STLTECH, GTPL, TTML

### textiles (15)
- **With F&O (1):** PAGEIND
- **Without F&O (14):** ARVIND, WELSPUNIND, TRIDENT, RAYMOND, VARDHACRLC, KPRMILL, NITINSPIN, GARFIBRES, RSWM, VTL, SNL, RUPA, ORIENTLTD, GOKEX
