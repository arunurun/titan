# NSE Sector Priority Reconciliation (2026-05-18)

## Scope and Method

This report ranks NSE-relevant sectors by **short-term money-making potential** (momentum + liquidity + news-flow sensitivity + breadth of tradable names), then reconciles that target taxonomy against the currently active sectors in Supabase.

Scoring model (0-100, practical heuristic):
- **Momentum transmission (35%)**: how quickly sector-level moves propagate to constituents.
- **Liquidity and participation depth (25%)**: ability to execute and rotate without slippage blowouts.
- **Catalyst frequency (25%)**: policy/events/earnings/macros that can trigger repeat opportunities.
- **Volatility-adjusted opportunity (15%)**: directional range usable for short-horizon setups.

Market-context assumptions use broadly accepted NSE behavior: cyclical leadership in defence/capex/power/PSU financials during domestic capex phases, periodic commodity beta in metals/oil-gas, and lower short-horizon beta in defensives (staples/insurance/telecom) except event windows.

---

## Ranked NSE Sector Priority (Top 20)

| Rank | Sector Key (Proposed Canonical) | Score | Practical Rationale (Short-Term Opportunity) |
|---|---|---:|---|
| 1 | ai | 92 | High headline velocity, thematic momentum, and frequent re-pricing in mid/small-cap basket names. |
| 2 | defence | 89 | Policy/orderbook catalysts and strong retail/institutional momentum participation. |
| 3 | power_utilities | 86 | Structural power demand + capex visibility; frequent momentum bursts in generation/T&D names. |
| 4 | capital_goods_industrials | 84 | Domestic manufacturing/capex cycle creates broad tradable leadership and rotation opportunities. |
| 5 | banks_psu | 82 | High beta to rate/liquidity narratives with strong trend persistence during risk-on phases. |
| 6 | infrastructure_construction | 81 | Tender/order-flow news and budget/capex triggers produce repeated setup windows. |
| 7 | nbfc_financial_services | 80 | Credit-cycle and retail flow sensitivity; good short-horizon swing breadth. |
| 8 | realty_reits | 78 | Strong cyclical beta to rates/liquidity and sentiment-driven fast trend moves. |
| 9 | metals_mining | 76 | Global commodity linkage creates tradable directional moves and relative-value rotations. |
| 10 | oil_gas_energy | 75 | Crude/spread sensitivity plus PSU energy rerating episodes provide tactical opportunities. |
| 11 | auto_ancillary | 74 | Supply-chain and OEM cycle pass-through yields high dispersion (good stock-picking momentum). |
| 12 | auto | 72 | Demand-cycle and margin commentary drive swing moves, though large-cap weights can dampen speed. |
| 13 | chemicals | 70 | Export cycle + input-cost spread shifts create periodic re-rating/reversal windows. |
| 14 | it | 68 | Tradable around US yields/currency and global tech sentiment, but often more index-driven. |
| 15 | pharma_healthcare | 66 | Event-driven bursts (USFDA, approvals) with moderate baseline momentum. |
| 16 | cement_building_materials | 64 | Infra/housing linked; opportunities rise around pricing cycles and dispatch trends. |
| 17 | logistics | 62 | Beneficiary of trade/capex flows; opportunity moderate due to uneven liquidity across names. |
| 18 | consumer_discretionary | 58 | Broad basket but mixed drivers; momentum less synchronized outside specific sub-themes. |
| 19 | media | 54 | High volatility but lower consistency/liquidity depth for sustained high-conviction runs. |
| 20 | fmcg_staples | 49 | Defensive profile; generally lower short-horizon momentum except earnings-surprise windows. |

Sectors not in Top 20 opportunity band: `banks_private`, `insurance`, `telecom`, `textiles` (kept in universe but lower immediate momentum priority).

---

## Top 5 Sectors for Immediate Cleanup After AI

1. `defence` - highest non-AI momentum persistence and strong catalyst cadence.
2. `power_utilities` - broad leadership, strong domestic macro tailwind, and frequent trend continuation.
3. `capital_goods_industrials` - capex-cycle breadth; large cleanup payoff across many symbols.
4. `banks_psu` - high-beta financial pocket with meaningful portfolio impact.
5. `infrastructure_construction` - policy/orderbook-driven moves; likely to benefit from taxonomy tightening.

---

## Supabase Current Sector Snapshot

Source used for active keys:
- Runtime introspection via `sector_registry.list_active_sector_ids(include_unknown=False)`
- Corroborated by `data/reports/live_sector_validation.json` (24 sectors observed)

Active Supabase sectors (24):
- `ai`
- `auto`
- `auto_ancillary`
- `banks_private`
- `banks_psu`
- `capital_goods_industrials`
- `cement_building_materials`
- `chemicals`
- `consumer_discretionary`
- `defence`
- `fmcg_staples`
- `infrastructure_construction`
- `insurance`
- `it`
- `logistics`
- `media`
- `metals_mining`
- `nbfc_financial_services`
- `oil_gas_energy`
- `pharma_healthcare`
- `power_utilities`
- `realty_reits`
- `telecom`
- `textiles`

---

## Reconciliation: Ranked Target vs Supabase

### A) Keep (already aligned)

`ai`, `defence`, `power_utilities`, `capital_goods_industrials`, `banks_psu`, `infrastructure_construction`, `nbfc_financial_services`, `realty_reits`, `metals_mining`, `oil_gas_energy`, `auto`, `auto_ancillary`, `chemicals`, `it`, `pharma_healthcare`, `cement_building_materials`, `logistics`, `consumer_discretionary`, `media`, `fmcg_staples`, `banks_private`, `insurance`, `telecom`, `textiles`

### B) Rename / Merge Suggestions (taxonomy hardening)

| Current | Proposed | Action | Why |
|---|---|---|---|
| `nbfc_financial_services` | `financials_nbfc` (alias accepted) | Rename (soft) | Improves consistency with financials grouping and future expansion. |
| `banks_private` + `banks_psu` | keep split + optional parent `banking` tag | Keep split, add parent tag | Split is useful for momentum regimes; parent tag helps aggregate analytics. |
| `capital_goods_industrials` + `infrastructure_construction` | keep split with strict mapping rules | No merge now | Both are high-priority but represent different value-chain stages. |
| `oil_gas_energy` + `power_utilities` | keep split | No merge now | Distinct drivers (commodity vs regulated/utility-capex dynamics). |
| `realty_reits` | `realty` (and optional `reits` secondary tag) | Rename (soft) | REIT set is small; primary sector should stay concise for operations. |

### C) Deprecate Candidates

No immediate deprecations recommended.  
Reason: all 24 sectors are still operationally relevant in an NSE coverage universe; low-momentum sectors should be **de-prioritized**, not removed, to avoid blind spots in rotations.

### D) Add Candidates (gaps for momentum tracking)

| Add Sector | Priority | Why Gap Matters |
|---|---:|---|
| `renewables_clean_energy` | High | Captures non-utility clean-energy beta otherwise diluted inside power/oil-gas. |
| `railways_transport_infra` | High | Rail/transport capex is a recurring momentum pocket not cleanly represented. |
| `electronics_ems` | Medium-High | Distinct manufacturing/PLI-driven theme with unique momentum behavior. |
| `healthcare_services` | Medium | Separates hospitals/diagnostics from pharma API/formulation cycles. |

---

## Action Matrix (Execution-Oriented)

| Sector | Priority Rank | DB Presence | Reconciliation Action |
|---|---:|---|---|
| `ai` | 1 | Yes | Keep (baseline already active) |
| `defence` | 2 | Yes | Keep + immediate cleanup |
| `power_utilities` | 3 | Yes | Keep + immediate cleanup |
| `capital_goods_industrials` | 4 | Yes | Keep + immediate cleanup |
| `banks_psu` | 5 | Yes | Keep + immediate cleanup |
| `infrastructure_construction` | 6 | Yes | Keep + immediate cleanup |
| `renewables_clean_energy` | — | No | Add |
| `railways_transport_infra` | — | No | Add |
| `electronics_ems` | — | No | Add |
| `realty_reits` | 8 | Yes | Soft rename to `realty` + alias |
| `nbfc_financial_services` | 7 | Yes | Soft rename to `financials_nbfc` + alias |

---

## Suggested Strict Curation Policy Template (Per Sector)

Use this template for every sector key (including existing and newly added):

1. **Eligibility**
   - Instrument must be listed on NSE (BSE optional mirror), equity only.
   - Exclude suspended, illiquid, or shell-like entities (configurable minimum liquidity thresholds).
2. **Primary Classification Rule**
   - Map by dominant revenue/profit driver (latest annual + trailing disclosures).
   - If conglomerate, assign to segment with highest earnings contribution; add secondary tags separately.
3. **Conflict Resolution**
   - Only one primary `sector_key` per instrument.
   - Overlap candidates go through manual override queue (`sector_overrides`) with reason log.
4. **Momentum-Readiness Filters**
   - Maintain minimum daily traded value threshold for priority workflows.
   - Flag low-data symbols but do not silently drop without audit trail.
5. **Naming and Taxonomy Controls**
   - `sector_key` lowercase snake_case only; maintain alias table for backward compatibility.
   - Rename operations must preserve historical mapping via alias + migration notes.
6. **Review Cadence**
   - Weekly automated validation run, monthly manual taxonomy review, quarterly structural re-benchmark.
   - Any sector with >10% unmapped/failed symbols enters mandatory cleanup cycle.

### Per-Sector Strict Addendum (Immediate Top 5 After AI)

| Sector | Strict Boundary Rule | Exclude From Sector |
|---|---|---|
| `defence` | Include companies with defence orderbook/material revenue as primary growth driver. | Generic engineering names without defence-dominant exposure. |
| `power_utilities` | Include generation, transmission, distribution, grid-utility ecosystem. | Upstream O&G and pure equipment names (route to energy/industrials). |
| `capital_goods_industrials` | Include industrial machinery, heavy equipment, process capital goods. | EPC-only infra project contractors (route to infra). |
| `banks_psu` | Include only PSU majority-owned banking entities. | NBFCs, insurers, private lenders. |
| `infrastructure_construction` | Include EPC, construction, infra developers/operators as core business. | Capital goods manufacturers selling into infra but not project executors. |

---

## Recommended Next Operational Step

After AI cleanup, execute cleanup in this order for highest immediate impact:
1. `defence`
2. `power_utilities`
3. `capital_goods_industrials`
4. `banks_psu`
5. `infrastructure_construction`

Then add the 3 high-priority missing buckets: `renewables_clean_energy`, `railways_transport_infra`, `electronics_ems`.
