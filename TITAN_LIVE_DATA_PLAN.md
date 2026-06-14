# Titan Live / Near-Real-Time Data Plan

Branch: `14thJune` · Created: 2026-06-14 · Owner of this doc: planning agent (planning only)

This is a **planning deliverable**. No code, SQL, Supabase data, or git state is changed by this
file. It is the only file written. It does not modify `PHASE1_FIX_CHECKLIST.md`,
`TITAN_DEEPSCAN_FIX_CHECKLIST.md`, or any script.

## Purpose

Titan today is an **EOD-batch** framework on GitHub Actions cron. The owner acts on a weekly
**winners shortlist** (`sector_daily_winners`) and on per-symbol action signals, both **intraday**
(during the session) and at **next-open**. The dominant failure is *"buy a Titan pick, then it
declines."* The two checklists trace this to (a) trailing-momentum rank picking already-extended
names that mean-revert, (b) a same-day-move pop that fades, (c) regime-driven misses (PSU banks
CANBK/PNB while Bank-Nifty rolled over), (d) low-delivery "churn" pops (DIXON), (e) sign-blind
absorption crediting high-volume **down** days, and (f) EOD-signal/next-open slippage.

Fix A (overextension penalty) and Fix C (same-day-pop de-bias) are already shipped on `14thJune`.
The remaining misses are **regime** and **intraday microstructure** problems that EOD data alone
cannot see in time. This plan adds a **live confirmation/gate layer** that runs as a small
always-on consumer, persists ticks/bars/regime snapshots to Supabase, and feeds the existing EOD
scoring as a **gate** (never a rewrite).

---

## What Titan already has (do NOT duplicate)

Grounded in the code on `14thJune`:

- **EOD cash OHLCV via Breeze REST** — `fetch_equity_data` (`src/breeze_client.py:581`) calls
  `breeze.get_historical_data(interval="1day", ...)`; NIFTY via `fetch_nifty_data` (`:780`).
- **A single polled REST quote** during an open session — `build_equity_live_audit`
  (`src/sector_audit.py:2880`) already calls `fetch_equity_quote` (`src/breeze_client.py:704`,
  `breeze.get_quotes(...)`) **when `ohlc_meta["session_open"]` is true** (`src/sector_audit.py:3101-3123`)
  and derives `session_move_vs_prev_close_pct` (`:3135`), `session_cmf_20`,
  `session_volume_participation_ratio`, and `price_snapshot_ts`. This is a **one-shot poll**, not a
  stream — there is no `ws_connect`/`subscribe_feeds` anywhere in the repo.
- **Option-chain OI via REST** — `fetch_option_metrics_with_expiry_fallback`
  (`src/breeze_client.py:483`); unavailable 84–93% of the time per the deep-scan.
- **EOD free-NSE feeds (just added)** — `src/nse_eod.py`: `sec_bhavdata_full` (OHLCV + **delivery
  qty/%**), `ind_close_all` (**India VIX** + index closes), `fo_secban` (**F&O ban list**),
  `fo_udiff` (**futures/options OI**), `fiidiiTradeReact` (**FII/DII cash**), corporate-actions
  calendar. **All inherently EOD / provisional.**
- **Breeze session management** with daily-expiry detection — `create_breeze_session`
  (`src/breeze_client.py:143-161`), REST rate-limit throttle `_reserve_breeze_call_slot`
  (`:115-135`, `BREEZE_HIST_CALL_INTERVAL_SECONDS`), and a reconcile guard that blocks Breeze
  fetches in Supabase-only mode (`_ensure_breeze_allowed`, `:54-59`).

**Implication:** delivery%, FII/DII, futures-OI-EOD, ban-list, VIX-close, and corp-actions are
already covered EOD and must **stay EOD**. What is missing is the **streaming / intraday** layer
and a **next-open** read — exactly the data needed for the regime, churn, gap, and short-covering
misses.

---

## Confirmed Breeze streaming capability (2026)

Verified against the ICICI Breeze SDK docs and `breeze-connect` v1.0.69 (PyPI) on 2026-06-14:

- **Connect:** `breeze.ws_connect()` opens the tick server; assign `breeze.on_ticks = callback`.
  The process must **stay alive** (the SDK closes the socket if the script exits) — i.e. a
  persistent consumer, *not* a cron job.
- **Streaming OHLCV by token:** `breeze.subscribe_feeds(stock_token="4.1!2885", interval="1minute")`.
  Valid intervals: `"1second"`, `"1minute"`, `"5minute"`, `"30minute"`. Token format is
  `<exchange>.<segment>!<scrip-code>` (e.g. `4.1!2885` = NSE cash RELIANCE; `4.1!NIFTY 50` = index).
  Sample tick: `{'interval':'1minute','exchange_code':'NSE','stock_code':'RELIND','open','high','low','close','volume','datetime'}`.
- **Streaming F&O OHLCV (carries OI):** same call on an NFO contract returns the 1-min bar **plus
  `oi`** — e.g. `{'exchange_code':'NFO','stock_code':'NIFTY','expiry_date',...,'volume','oi','datetime'}`.
- **Streaming quotes / index quotes:**
  `breeze.subscribe_feeds(exchange_code="NSE", stock_code="NIFTY", product_type="cash", get_market_depth=False, get_exchange_quotes=True)`.
  Quote tick is rich: `{'symbol','last','open','high','low','change','close','ttq' (total traded qty),
  'totalBuyQt','totalSellQ','avgPrice' (VWAP),'lowerCktLm','upperCktLm','OI','CHNGOI','ltt',...}`.
  For an index the price/levels populate (`last`, `change`) while depth fields are `None`.
- **Streaming market depth:** `get_market_depth=True` returns 5-level bid/ask
  (`BestBuyRate-1..5`, `BestBuyQty-1..5`, `BestSellRate-1..5`, ...).
- **Constraints:** at least one of `get_exchange_quotes` / `get_market_depth` must be `True`;
  a **static IP is mandatory** for Breeze API; the **session token expires daily** (same expiry the
  REST path already handles at `src/breeze_client.py:152-160`); there is a combined API rate limit
  and a per-subscription footprint, so the subscribed universe must be bounded.

Two especially valuable, **free-with-the-quote-stream** fields for Titan's misses: `avgPrice`
(running **VWAP**) and `totalBuyQt`/`totalSellQ` (**order-book imbalance**), plus `OI`/`CHNGOI` on
F&O streams (**real-time OI change**).

---

## 1. Live / near-real-time data sources to add (prioritized)

Universe scoping (keeps all sources within rate/IP limits): subscribe only to the **active
watchlist** — the union of the persisted priority/winners names (`load_priority_instruments`,
`src/sector_priority.py:2385`) and currently-held positions — plus a fixed **regime set** of
indices. This is tens of symbols, not the whole market.

### (a) Breeze streaming 1-minute OHLCV for the watchlist / F&O names — **P1**
- **What:** rolling 1-min `open/high/low/close/volume` (and `oi` for F&O contracts) per subscribed
  symbol; from these the consumer derives **session VWAP**, **cumulative-volume-vs-typical**, and
  **distance-from-VWAP** intraday.
- **Exact call:** `breeze.subscribe_feeds(stock_token="4.1!<scrip>", interval="1minute")` for cash;
  for F&O underlyings use the NFO contract subscription form
  (`exchange_code="NFO", stock_code=<code>, product_type="futures", expiry_date=<DD-MMM-YYYY>, get_exchange_quotes=True, get_market_depth=False, interval="1minute"`).
  Token resolution reuses the existing scrip resolver (`resolve_breeze_stock_code`,
  `breeze_scrip_master`) and the F&O code map (`load_fno_breeze_mapping`, `src/breeze_client.py:241`).
- **Cadence:** event-driven (one packet per closed 1-min bar). Persist each completed bar.
- **Storage:** `intraday_bars` (one row per symbol+minute); roll up to a per-symbol
  `live_intraday_features` snapshot (latest VWAP, cum-volume ratio, dist-from-VWAP) for cheap reads
  by the EOD layer.

### (b) Breeze streaming index quotes for a LIVE regime read — **P1 (highest leverage)**
- **What:** real-time level + `change` for **NIFTY 50, NIFTY BANK**, and the sector indices that map
  to Titan sectors (e.g. NIFTY PSU BANK for the CANBK/PNB case, plus the registry's sector indices).
  Used to compute a **live regime score** (each index vs its own intraday open / prior close /
  short EMA) and an intraday **breadth/▲▼ tilt**.
- **Exact call:** `breeze.subscribe_feeds(exchange_code="NSE", stock_code="NIFTY", product_type="cash", get_market_depth=False, get_exchange_quotes=True)` (and one per index, e.g. `stock_code="CNXBAN"`/`"NIFTY BANK"`).
- **Cadence:** continuous quote ticks; snapshot the derived regime every ~30–60 s.
- **Storage:** `live_regime_snapshots` (timestamp, index, last, pct_vs_prev_close, pct_vs_open,
  slope_proxy, regime_state ∈ {risk_on, neutral, risk_off, rolling_over}).

### (c) Live market depth / quotes (per-name confirmation) — **P2**
- **What:** per-watchlist-name quote stream giving **VWAP (`avgPrice`)**, **order-book imbalance**
  (`totalBuyQt` vs `totalSellQ`), `ttq`, and **circuit bands** (`lowerCktLm`/`upperCktLm`); optional
  5-level depth for the most liquid names.
- **Exact call:** `breeze.subscribe_feeds(exchange_code="NSE", stock_code="<CODE>", product_type="cash", get_exchange_quotes=True, get_market_depth=False)` (set `get_market_depth=True` only for a small high-priority subset).
- **Cadence:** continuous; snapshot every ~30–60 s alongside the 1-min bar.
- **Storage:** fold into `live_intraday_features` (vwap, buy_sell_imbalance, near_circuit flag).

### (d) Near-real-time intraday OI / delivery proxies — **P2/P3**
- **What:** for F&O names, **intraday futures `oi`/`CHNGOI`** straight off the stream → real-time
  long-buildup vs short-covering classification. Delivery% has **no intraday source** (it is an EOD
  archive in `sec_bhavdata_full`); the live proxy for "churn" is **intraday VWAP fade + front-loaded
  volume + low order-book follow-through**, reconciled against true delivery% post-close.
- **Exact call:** the F&O `subscribe_feeds(..., interval="1minute")` from (a) already carries `oi`;
  no extra call needed.
- **Cadence:** per 1-min bar.
- **Storage:** `live_intraday_features.oi`, `oi_change_pct`, `oi_price_regime`
  ∈ {long_buildup, short_covering, long_unwinding, short_buildup}.

---

## 2. How each source lifts efficacy (mapped to diagnosed failure modes + 12-stock evidence)

Each row ties a live source to a **specific** documented miss and states the expected gain and how
to measure it. Evidence references are from `PHASE1_FIX_CHECKLIST.md` and
`TITAN_DEEPSCAN_FIX_CHECKLIST.md`.

### 2.1 Live index regime → PSU-bank / hostile-regime misses (CANBK, PNB)
- **Failure mode:** Fix A (overextension) could **not** catch CANBK/PNB — they were *not*
  statistically extended; they fell because **Bank-Nifty rolled over** while breadth stayed high
  (the deferred "Fix B regime gate"; `PHASE1_FIX_CHECKLIST.md:50-52`,
  `TITAN_DEEPSCAN_FIX_CHECKLIST.md:150-167`). EOD breadth was 80–92% even as intent fell.
- **How live helps:** a **live NIFTY BANK / PSU-BANK regime read** (source b) flips to
  `rolling_over` intraday when the index breaks its open/prior-close and short slope turns negative —
  *hours before* the EOD rollup updates. The buy-gate consumes `live_regime_snapshots` for the
  symbol's sector and **withholds/half-sizes** new longs when that sector index is `risk_off`/
  `rolling_over`. This is the real-time complement to the deferred Fix B.
- **Expected gain:** removes the regime class of misses (2 of 6 named decliners). Qualitatively the
  largest single lift because Fix A structurally cannot address it.
- **Measure:** forward +1/+5-session win-rate of buys issued while sector-index regime = risk-off,
  before vs after the gate (expect the risk-off bucket's win-rate to rise toward the risk-on bucket).

### 2.2 Intraday VWAP / volume-churn proxy → low-delivery "churn" pop (DIXON)
- **Failure mode:** DIXON was an honest miss — stretch only **2.55**, so the overextension penalty
  barely touched it; it was a **low-conviction churn pop** that needs **delivery%**, which is only
  available **after close** (`PHASE1_FIX_CHECKLIST.md:42-44, 55-56`).
- **How live helps:** before EOD delivery% exists, a **streaming churn proxy** (sources a+c) flags
  the same pathology in real time: price up on the day but **trading below/ fading from VWAP**, with
  **front-loaded volume** (high early `ttq`, decelerating) and **weak order-book follow-through**
  (`totalBuyQt` not outpacing `totalSellQ`). That pattern = churn, not accumulation. The gate damps
  the buy intraday; the **post-close reconcile** then confirms against true `deliv_per`
  (`src/nse_eod.py:140-184`) and trains the proxy threshold.
- **Expected gain:** catches the churn-pop class (DIXON-type) a day earlier than the EOD delivery
  gate; reduces same-day-pop buys that fade next session.
- **Measure:** correlation of the live churn-proxy flag with next-day `deliv_per` being low; forward
  win-rate of buys with vs without the churn flag.

### 2.3 Next-open gap guard → EOD-signal / next-open fade (slippage)
- **Failure mode:** Titan's signals are computed on the **prior close**, but the owner often buys at
  **next-open**. A gap-up open means the EOD edge is partly gone before entry; a gap-down can trip
  the very risk the signal missed. The designed next-open/gap guard is **deferred**
  (`PHASE1_FIX_CHECKLIST.md:65`). Note Layer-B Tier-1 already uses a *trailing* `gap_down_proxy`
  (`src/sector_audit.py:3089-3094`, consumed at `src/signal_v2.py:503-515`) but nothing reads the
  **actual** open in real time.
- **How live helps:** at 09:15–09:20 the index + per-name quote stream gives the **real opening
  print**. A **gap guard** compares actual open vs the close the signal was built on: if a `buy`
  name gaps up beyond a threshold (edge front-run) → **defer or scale down**; if it gaps down into
  structural-break territory → **escalate** toward the existing Tier-1 path. This makes the
  inert/`prior_label`-less hysteresis and the gap logic in `signal_v2` actually fire on live opens.
- **Expected gain:** reduces entry slippage on gap-ups and avoids buying into gap-down traps;
  directly attacks the "buy then it declines from the open" complaint.
- **Measure:** average entry-to-+1-session return for gapped names with vs without the guard; track
  realized slippage (signal-close → actual entry) before/after.

### 2.4 Intraday futures OI → short-covering-pop vs long-buildup (real time)
- **Failure mode:** a pop driven by **short covering** (price up, OI down) is unsustainable and
  mean-reverts; a pop on **fresh longs** (price up, OI up) is healthier. Today this is only knowable
  EOD from `fo_udiff` (`src/nse_eod.py:293`), and the absorption term is **sign-blind** (deep-scan
  P0-2), so a climactic move can crown a name #1 (BDL, HAL, BEML, NETWEB evidence,
  `TITAN_DEEPSCAN_FIX_CHECKLIST.md:62-87`).
- **How live helps:** the F&O 1-min stream's `oi`/`CHNGOI` (source d) classifies the intraday move
  as `short_covering` / `long_buildup` in real time. A `short_covering` classification **tempers**
  the absorption/momentum contribution for that name's live gate (complementing the EOD P0-2 cap +
  sign-gate rather than replacing it).
- **Expected gain:** demotes short-covering pops among F&O winners before they are acted on;
  reinforces P0-2 for the F&O subset.
- **Measure:** forward win-rate of F&O buys tagged `short_covering` vs `long_buildup` (expect the
  former materially lower, and the gate should suppress them).

### 2.5 Live order-book imbalance / circuit bands → thin / trap confirmation
- **Failure mode:** thin-liquidity and trap-exit proxies are trailing/coarse
  (`liquidity_thin_proxy`, `trap_exit_proxy` in `build_equity_live_audit`).
- **How live helps:** `totalBuyQt`/`totalSellQ` imbalance and proximity to `upperCktLm`/`lowerCktLm`
  (source c) give a real-time confirmation that a pop has no depth behind it or is pinned at a
  circuit — a cheap corroborator for the live gate.
- **Expected gain:** incremental; mainly improves confidence calibration and avoids buying into
  circuit-locked illiquid prints.
- **Measure:** included in the aggregate before/after win-rate; track false-positive buys near
  circuit bands.

### 2.6 Common measurement harness (ties to deep-scan P1-1)
All efficacy claims are measured with **forward** returns, not the current circular same-day
hit-rate (`src/analysis_store.py:1395-1405`, `src/signal_v2_backtest.py:358-377`). Run the live
gate in **shadow mode** first (compute + log the gate decision without enforcing it), then compute
per-buy **+1/+5-session win-rate** and **post-signal max drawdown** for gated-vs-ungated cohorts.
The live layer is judged a win when the risk-off / churn / short-covering / gap-up cohorts show
higher forward win-rate (and smaller drawdown) once the gate is enforced.

---

## 3. Architecture (persistent consumer + Supabase + gate into EOD)

### 3.1 Where the consumer runs (it cannot be GitHub-Actions cron)
A websocket needs a **long-running** process (the SDK requires the script to stay alive;
`ws_connect()` + an idle loop). Cron jobs are short-lived, so this is a new deployment surface.
Options, in order of recommended fit:

1. **Small always-on cloud worker / container (recommended).** A single tiny VM or container
   (Fly.io / Railway / Render / a micro VM) running one consumer process, 24×5 during market hours.
   Pros: stable **static IP** (mandatory for Breeze), independent of the owner's PC, easy restart
   policy. Cons: a small recurring cost; must hold the daily session token.
2. **Local Windows service / Task Scheduler (owner is on Windows).** Run the consumer as a Windows
   scheduled task or service that auto-starts at 09:00 IST and auto-restarts on crash. Pros: zero
   cloud cost, reuses the owner's existing static IP if registered. Cons: depends on the PC being
   on; home IP must be the Breeze-registered static IP.
3. **(Not recommended) keep-warm GitHub Actions.** A 6-hour max job that re-launches every cycle is
   fragile, wastes minutes, and fights the runner's dynamic IP — explicitly out of scope.

The consumer is a **new standalone module** (e.g. `src/live_stream_consumer.py`) that imports the
existing `create_breeze_session` (`src/breeze_client.py:143`) for auth, then uses `ws_connect()` /
`subscribe_feeds()` / `on_ticks`. It must run **outside reconcile mode** (the
`_ensure_breeze_allowed` guard at `src/breeze_client.py:54-59` blocks Breeze in Supabase-only runs).

### 3.2 Tick buffering and persistence
- `on_ticks` pushes each packet onto an in-memory queue; a writer thread **batches** (e.g. every
  2–5 s or N rows) and bulk-inserts to Supabase to respect API limits. Never insert one row per tick.
- **New tables (DATA agent owns DDL; listed here for the plan only):**
  - `live_ticks` — raw quote/depth packets (symbol, ts, last, ttq, totalBuyQt, totalSellQ, oi,
    chngoi, lower_ckt, upper_ckt) for audit/replay; short retention (e.g. rolling 5–10 trading days).
  - `intraday_bars` — completed 1-min OHLCV(+oi) bars per symbol; medium retention.
  - `live_intraday_features` — latest per-symbol derived snapshot (vwap, dist_from_vwap_pct,
    cum_volume_ratio, buy_sell_imbalance, oi_change_pct, oi_price_regime, near_circuit, churn_flag).
  - `live_regime_snapshots` — per-index regime state used by the gate.
- The **gate-facing reads** are only `live_intraday_features` and `live_regime_snapshots` (tiny,
  indexed by symbol/sector + ts), so the EOD path stays cheap.

### 3.3 Reconnect / heartbeat / daily session expiry
- **Heartbeat:** track last-tick timestamp; if no ticks for ~30 s during market hours, force
  `ws_disconnect()` → `ws_connect()` → re-`subscribe_feeds` the full universe.
- **Reconnect/backoff:** exponential backoff on socket close (mirror the REST retry/backoff style in
  `fetch_equity_data`, `src/breeze_client.py:643-656`).
- **Daily session refresh:** the Breeze **session token expires daily** (already detected for REST
  at `src/breeze_client.py:152-160`). The consumer starts each trading day by generating a fresh
  session before `ws_connect()`; on an auth/"session expired" error it **stops cleanly and alerts**
  (the token is obtained via the existing manual `scripts/breeze_session.py` flow — do not automate
  credential changes here).
- **Market-hours gating:** only subscribe 09:08–15:35 IST; outside that, idle/sleep.

### 3.4 How the live layer feeds the existing EOD scoring (gate, not rewrite)
The live layer is a **confirmation/gate** that sits *in front of* publication and entry — the EOD
engines are unchanged in spirit:

- **Winners publication gate.** Before `persist_daily_winners` (`src/sector_priority.py:2440`)
  publishes a name, an optional step reads `live_regime_snapshots` for the name's sector and
  `live_intraday_features` for the name; it **down-ranks/withholds** names whose sector is
  `risk_off`/`rolling_over` or that carry a live `churn_flag` / `short_covering` tag. This composes
  with the deep-scan P1-6 idea (gate winners by `signal_v2` risk labels) rather than competing.
- **`signal_v2` buy-gate confirmation.** The buy gate `_buy_gate` / `_map_label`
  (`src/signal_v2.py:604-647`) gains an **optional live-confirmation input** (behind an env flag,
  NaN-safe): when live regime is hostile or the gap guard trips, the constructive label is capped to
  `accumulate`/`hold` via the existing `_apply_ceiling` mechanism (`src/signal_v2.py:650-654`); a
  live structural gap-down can feed the existing Tier-1 escalation (`_escalate`, `:657-660`). No new
  scoring math — it reuses the ceiling/escalation hooks already present.
- **Strict EOD/DATA separation.** The live consumer only **writes** the `live_*` tables; the EOD
  scoring only **reads** them behind an env flag with a NaN-safe fallback, exactly the pattern the
  deep-scan prescribes for new feeds (`TITAN_DEEPSCAN_FIX_CHECKLIST.md:258-275`). If the live layer
  is down, Titan degrades gracefully to today's EOD behaviour.

---

## 4. Phased rollout (tasks — not implemented)

Effort: **S** (<½ day) / **M** (½–2 days) / **L** (>2 days). Each task lists dependency and the
efficacy it unlocks. Nothing here is built yet.

### Phase 1 — highest leverage, lowest effort: live regime + next-open gap guard
The cheapest live signals that attack the biggest misses, on the **existing watchlist** only.

- **P1-a · Stand up the persistent consumer skeleton.** New `src/live_stream_consumer.py`: reuse
  `create_breeze_session`, `ws_connect()`, an idle loop, heartbeat/reconnect, daily-session refresh,
  batched writer. Subscribe **indices only** to start. *Effort:* M. *Depends on:* deployment surface
  (3.1) + static IP + daily token. *Unlocks:* infra for everything below.
- **P1-b · Live regime snapshots + sector mapping.** Derive `regime_state` per index; map indices to
  Titan sectors (esp. NIFTY BANK / PSU BANK → PSU-bank sector). Persist `live_regime_snapshots`.
  *Effort:* S–M. *Depends on:* P1-a. *Unlocks:* §2.1 (CANBK/PNB-class regime misses) — the single
  biggest qualitative lift.
- **P1-c · Next-open gap guard.** At the open, compare actual opening print (index + per-name quote)
  vs the signal's reference close; expose a `gap_state` the EOD buy-gate can read via the existing
  ceiling/escalation hooks. *Effort:* S–M. *Depends on:* P1-a (+ a thin per-name quote subscription).
  *Unlocks:* §2.3 (EOD→next-open slippage).
- **P1-d · Shadow-mode wiring + forward-return measurement.** Read `live_regime_snapshots` in the
  winners publish / buy-gate path **without enforcing**, log the would-be decision, and add the
  forward +1/+5 win-rate harness (P1-1 alignment). *Effort:* M. *Depends on:* P1-b/P1-c.
  *Unlocks:* the evidence to safely turn enforcement on.

### Phase 2 — per-name microstructure: churn proxy, VWAP, order-book, intraday OI
- **P2-a · Per-name 1-min OHLCV + VWAP/volume features.** Subscribe the watchlist; persist
  `intraday_bars` + `live_intraday_features` (vwap, dist_from_vwap, cum_volume_ratio). *Effort:* M.
  *Depends on:* P1-a. *Unlocks:* §2.2 churn proxy foundation.
- **P2-b · Churn-pop flag + post-close delivery reconcile.** Compute `churn_flag` from VWAP-fade +
  front-loaded volume + weak imbalance; reconcile against EOD `deliv_per` (`src/nse_eod.py`) to
  calibrate thresholds. *Effort:* M. *Depends on:* P2-a + EOD delivery feed. *Unlocks:* §2.2 (DIXON).
- **P2-c · Intraday futures OI regime.** From the F&O 1-min `oi`/`CHNGOI` stream, classify
  `oi_price_regime`; expose `short_covering` as a temper on the F&O live gate. *Effort:* M.
  *Depends on:* P2-a + F&O code map. *Unlocks:* §2.4 (BDL/HAL/BEML-class short-covering pops;
  complements P0-2).
- **P2-d · Order-book imbalance / circuit corroborator.** Persist `buy_sell_imbalance`, `near_circuit`
  into `live_intraday_features`. *Effort:* S. *Depends on:* P2-a. *Unlocks:* §2.5.

### Phase 3 — enforce, harden, scale
- **P3-a · Flip gates from shadow to enforce.** Behind env flags, enable winners down-rank/withhold
  and the buy-gate ceiling/escalation, **only** after Phase-1/2 shadow metrics show net positive.
  *Effort:* S. *Depends on:* P1-d evidence. *Unlocks:* realized efficacy gains.
- **P3-b · Resilience + observability.** Tick-gap alerts, reconnect dashboards, dropped-symbol
  detection, session-expiry alerting, retention/rollup jobs for `live_ticks`/`intraday_bars`.
  *Effort:* M. *Depends on:* Phase 1–2. *Unlocks:* trustable always-on operation.
- **P3-c · Universe scaling.** Grow the subscribed set if rate/IP budget allows; prioritize F&O
  names and held positions. *Effort:* S–M. *Depends on:* P3-b.

---

## 5. Risks and limits

- **Breeze streaming limits / static IP / ToS.** A **static IP is mandatory**; the Breeze-registered
  IP must match the consumer's host (favours a fixed cloud worker). There is a combined API rate
  limit and a practical cap on simultaneous subscriptions → **bound the universe** to the watchlist +
  regime set; do not subscribe the whole market. Respect ICICI API ToS (data is for the account
  holder's own use).
- **Daily session-token expiry.** The token expires daily and is obtained via a manual browser login
  (`scripts/breeze_session.py`, surfaced at `src/breeze_client.py:152-160`). The consumer must
  refresh at start-of-day and **alert + halt** on expiry — it must not silently serve stale data, and
  this plan does **not** automate credential rotation.
- **Always-on infra cost & ops.** A persistent worker is a new operational surface (uptime,
  restarts, monitoring) absent from the current cron model — a real but small recurring cost and some
  maintenance burden.
- **Added complexity vs. the EOD model.** The live layer must stay a **thin gate** with NaN-safe
  fallback; if it is down, Titan must behave exactly as today. Avoid letting live logic creep into
  the core scoring math (keep it to the existing ceiling/escalation/withhold hooks).
- **Intraday noise / overfitting.** 1-min signals are noisier than EOD; thresholds (churn, regime
  slope, gap) must be calibrated against forward outcomes and reconciled to EOD truth (delivery%,
  EOD OI) before enforcement — hence the mandatory **shadow-mode** phase.
- **What must stay EOD (do not stream).** **Delivery %** (no intraday source), **FII/DII cash**
  (published post-close, provisional), **EOD futures-OI/basis snapshots**, **ban-list**, **India VIX
  close**, and **corporate-actions/results calendar** are inherently EOD/provisional and remain in
  `src/nse_eod.py`. The live layer **proxies** them intraday (e.g. churn proxy for delivery, live OI
  for EOD OI) and **reconciles** to the EOD truth after close — it never claims to replace them.

---

## Summary

The "buy-then-decline" residue after Fix A/C is dominated by **regime** (CANBK/PNB), **intraday
churn** (DIXON), **short-covering pops** (BDL/HAL/BEML class), and **EOD→next-open slippage** —
none of which EOD data can see in time. Breeze's confirmed websocket (`ws_connect` +
`subscribe_feeds`) supplies exactly the missing signals: **live index regime**, **1-min VWAP/volume
churn**, **real opening prints**, and **intraday futures OI**. Run it as a **small always-on
consumer** (cloud worker or Windows service, static IP, daily session refresh), persist to compact
`live_*` Supabase tables, and feed the existing winners/`signal_v2` gates as a **NaN-safe
confirmation layer**. Phase 1 (live regime + gap guard, shadow-measured) is the highest-leverage,
lowest-effort start; delivery%, FII/DII, EOD OI, ban-list, VIX, and the calendar stay EOD.
