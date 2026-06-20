# Titan Stock-Analysis Framework — Methodology Review (Self-Contained)

> **Purpose.** This document transcribes the *actual* logic of the Titan stock-analysis
> framework (Indian equities, NSE/BSE) directly from source code so that a reviewer with **no
> access to the codebase** can audit the methodology. Every formula, weight, threshold, and
> waterfall layer is cited as `file:line` against the repository it was read from. Where the
> in-repo docs disagree with the code, the **code is treated as ground truth** and the drift is
> flagged in §8.
>
> **Conventions used throughout**
> - All price/volume metrics are computed on a trailing daily OHLCV `DataFrame` (`df`), newest row
>   last. No look-ahead: every metric uses only data up to and including the latest row.
> - "Returns" are close-to-close **percent** returns (`-2.0` means −2%).
> - `NaN` is the explicit "unavailable" sentinel; helpers coerce non-numerics to `NaN` and treat
>   `NaN` as "no contribution".
> - The **audit dict** is the single shared payload. `src/sector_audit.py` computes metrics and
>   writes them onto the audit; `src/signal_v2.py` consumes the audit to produce a label.
> - Line numbers are from the source at review time; small drift is possible if files change.

---

## 1. Overview — end-to-end pipeline

Titan ranks stocks within a sector ("who are the winners?") and then assigns each name a
forward-looking action label ("buy / accumulate / hold / trim / exit-risk"). It also reconciles
past predictions against realized returns to measure hit-rate.

```
                    ┌─────────────────────────────────────────────────────────────┐
 DATA SOURCES       │  Breeze (OHLCV cash bars, option chains, live quote)         │
                    │  NSE / Moneycontrol / Screener / Yahoo (market cap)          │
                    │  Google News RSS + NSE bulk/block/announcements (news)       │
                    │  yfinance (precious-metals macro: GOLD, SILVER, DXY)         │
                    └───────────────────────────┬─────────────────────────────────┘
                                                 │
              ┌──────────────────────────────────┴───────────────────────────────┐
              ▼                                                                    ▼
 (A) SECTOR RANKING  src/sector_priority.py                  (B) PER-SYMBOL FEATURES  src/sector_audit.py
   per stock: cap_bias + return weights                        z_score, cmf_20, adx_14, obv_slope_20,
   + absorption term − overextension penalty                   ema_200_distance_pct, atr_14_pct,
   + sector news blend  =>  rank_score                         ema200_stretch_atr, returns, proxies,
   sort, tag micro/small-cap "priority" winners,               intent/effective_intent_score,
   persist to sector_priority_rankings / sector_daily_winners  next_day_score / next_week_score,
                                                                sector percentiles, options walls
              │                                                                    │
              │                                                                    ▼
              │                                       (C) SIGNAL ENGINE  src/signal_v2.py (A–E waterfall)
              │                                          Layer A data quality (withhold/ceiling)
              │                                          Layer C graded bear/bull evidence (ramps)
              │                                          Layer D context modifiers (reshape weights)
              │                                          Layer B two-tier hard disqualifiers
              │                                          Layer E aggregate -> risk_net -> label
              │                                          => action_signal + confidence + reason trace
              ▼                                                                    ▼
 (D) RECONCILIATION  src/analysis_store.py / src/reconcile_runner.py / src/signal_v2_backtest.py
    compare stored prediction direction vs realized next-day return direction => hit-rate,
    drawdown-saved, false-exit cost, flip-rate, confidence calibration
```

**Two distinct scoring systems** (reconciled in ranking since Jun 2026):
1. `rank_score` (§3) — a *cross-sectional sector ranking* score. Higher = stronger winner.
   Includes a `v2_rank_adjustment` term derived from the latest stored `signal_v2` label /
   `risk_net` so offensive momentum and defensive posture move together.
2. `risk_net` + label (§4–5) — a *per-stock defensive/constructive* signal. Higher risk_net = more
   defensive.

The action labels are also routed through `action_signals.derive_action_signal`, which always
calls `evaluate_signal_v2` (`src/action_signals.py:129-133`). A legacy discrete-step scorer
`_derive_action_signal_legacy` (`src/action_signals.py:136-323`) still exists but is used **only**
by the A/B backtest harness as a reference baseline.

---

## 2. Feature / metric glossary

All "computed in" references point at the pure helpers in `src/titan_engine.py` and
`src/tape_metrics.py`, with the audit-field assembly in `src/sector_audit.py`
(`build_equity_live_audit`, audit dict at `:3103-3199`).

| Metric (audit key) | One-line definition | Computed in |
|---|---|---|
| `z_score` | Blended rolling z-score of last close vs trailing mean/σ (population σ); `0.55*z_fast(20) + 0.45*z_slow(≤60)` when ≥45 sessions, else fast only. | `calculate_z_score` `titan_engine.py:12-25`; blend in `sector_audit.py` |
| `z_score_fast_20` | 20-session rolling z-score. | `titan_engine.py:12-25` |
| `cmf_20` | Chaikin Money Flow over 20 sessions; `Σ(MFM·vol)/Σ(vol)`, MFM `= ((C−L)−(H−C))/(H−L)`. Range [−1,+1]. | `calculate_cmf` `titan_engine.py:176-200` |
| `obv_slope_20` | Least-squares slope of On-Balance-Volume over last 20 sessions; only its **sign** is used. | `calculate_obv_slope` `titan_engine.py:203-230` |
| `adx_14` | Average Directional Index (14), rolling-sum approximation of Wilder smoothing. Range [0,100]. | `calculate_adx` `titan_engine.py:60-99` |
| `adx_plus_di_14`, `adx_minus_di_14` | Latest +DI / −DI directional indicators. | `calculate_latest_di` `titan_engine.py:102-136` |
| `ema_200` | 200-span EMA of close (`ewm(adjust=False)`, α=2/201). | `calculate_ema` `titan_engine.py:28-35` |
| `ema_200_distance_pct` | `(close_last/ema_200 − 1)·100`. Positive = above 200-EMA. | `sector_audit.py:2988-2992` (region) |
| `atr_14` | Average True Range (14) = SMA of True Range (not Wilder EMA). | `calculate_atr` `titan_engine.py:38-57` |
| `atr_14_pct` | `(atr_14/close_last)·100`. Volatility as % of price. | `sector_audit.py:3002-3006` |
| `atr_14_over_atr_63` | ATR(14)/ATR(63); >1 = short-term vol expansion. | `calculate_atr_ratio` `titan_engine.py:167-173` |
| `ema200_stretch_atr` (a.k.a. `stretch`) | **Signed** over-extension: `ema_200_distance_pct / atr_14_pct` (how many ATR-% the price sits from its 200-EMA). | `sector_audit.py:3055-3063` |
| `atr_break_multiple` | `|close_last − ema_200| / atr_14` (ATRs of displacement from EMA200). | `sector_audit.py:3007-3016` |
| `return_1d_pct` | `(close_last/close_prev − 1)·100`. | `sector_audit.py` (region ~3130) |
| `return_5d/10d/20d_pct` | Close-to-close % over n sessions. | `pct_return_n_sessions_back` `tape_metrics.py:37-46` |
| `return_1w_pct` / `return_1m_pct` (ranking) | 5-session / 20-session % returns used by the sector ranking module. | `_return_pct` `sector_priority.py:1973-1981` |
| `rel_return_5d/10d/20d_vs_nifty_pct` | Stock minus benchmark % over same dates (inner-join on date). | `benchmark_relative_returns` `tape_metrics.py:91-135` |
| `session_move_vs_prev_close_pct` | Intraday live-quote move vs prior close (when session open). | `sector_audit.py:3074,3088,3113` |
| `volume_participation_ratio` (VPR) / `absorption_ratio` | Today's volume ÷ mean of prior-5-session volume. >1 above-average turnover. (Aliased; in the **ranking** module the same idea is `absorption`.) | `volume_participation_ratio` (breeze_client); audit `:3121,3125` |
| `intent_score` / `effective_intent_score` / `equity_technical_score` | Cash-market composite 0–100 from z + volume participation: `100·(0.52·norm_z + 0.48·norm_participation)`. `effective_intent_score` is later mutated down by guardrails. | `calculate_equity_technical_score` `titan_engine.py:326-351`; assigned `sector_audit.py:3045,3179-3181` |
| `next_day_score` / `next_week_score` | Heuristic forward composites (0–100, baseline 50) from tech composite + returns + rel-returns + EMA distance − ATR penalty. | `_predictive_scores` `sector_audit.py:1885-1989` |
| `atr_penalty_input` | Sector-relative volatility input: `min(5.0, atr_14_pct / sector_median_atr_14_pct)` (falls back to raw `atr_14_pct`). | `sector_audit.py:2755-2766` |
| `sector_pctile_ema200_stretch`, `sector_pctile_next_week_score`, etc. | Empirical 0–100 percentile of a metric across the sector cohort (mid-rank ties). | `percentile_rank_0_100` `tape_metrics.py:20-34`; assigned `sector_audit.py:2739-2782` |
| `median_notional_inr_20d` | Median of `close·volume` over last 20 rows (liquidity gauge). | `median_notional_inr_20d` `tape_metrics.py:49-59` |
| `pcr` / `put_oi` / `call_oi` / `put_oi_wall_strike` / `call_oi_wall_strike` | Option-chain context (F&O names only): put-call OI ratio and max-OI strikes ("walls"). | `options_context.py:82-141`; `get_pcr`/`find_call_put_oi_walls` `titan_engine.py:242-289` |
| `news_count` / `news_sentiment_score` / sector news `score` | Heuristic news sentiment×impact×confidence blended per sector theme. | `sector_priority.py:1224-1355`; `news_sentiment.py` (VADER/FinBERT) |
| Boolean proxies | `high_volume_down_day_proxy`, `panic_absorption_proxy`, `trap_exit_proxy`, `structural_break_proxy`, `gap_down_proxy`, `extreme_price_move_proxy`, `liquidity_thin_proxy`, `history_lt_200_sessions`, `event_risk_soon`. See §6. | `sector_audit.py:3046-3162` |

### 2.1 Key composite formulas (transcribed)

**Cash-market composite** (`calculate_equity_technical_score`, `titan_engine.py:326-351`):
```
norm_z(z)             = clamp01(0.5 + 0.5*tanh(z/3))          (NaN -> 0.5)
norm_participation(p) = clamp01(p/3)   (+inf -> 1.0, NaN -> 0.5)
score = round(100 * (0.52*norm_z + 0.48*norm_participation), 2)     # 0..100
```
There is also an index/NIFTY variant `calculate_intent_score(pcr, z, absorption)` with weights
`0.35/0.35/0.30` (`titan_engine.py:292-323`) — **not** used for single-stock cash equities.

**Forward composites** (`_predictive_scores`, `sector_audit.py:1885-1989`). `tech =
effective_intent_score`, `atr_in = atr_penalty_input` (fallback `atr_14_pct`),
`ema_conf = _ema_history_confidence(rows)`:
```
tech_day  = (tech-50)*0.52         tech_week = (tech-50)*0.62        (0 if tech NaN)
ret1_w    = 0.18 if extreme_price_move_proxy else 0.42
ret_term  = ret1d * ret1_w          # upside slice shaved by contemporaneous discount
ret5_term = ret5d * 0.28            ret10_term = ret10d * 0.15
rel5_term = rel5 * 0.20             rel20_term = rel20 * 0.11
ema_base  = ema_200_distance_pct * 0.26 * ema_conf
ema_day   = ema_base * 0.85         ema_week  = ema_base * 1.0
atr_penalty = atr_in * 0.45

next_day  = 50 + tech_day  + ret_term      + ret5_term      + 0.55*ret10_term
               + rel5_term + 0.55*rel20_term + ema_day  - atr_penalty
next_week = 50 + tech_week + 0.78*ret_term + 0.82*ret5_term + 0.48*ret10_term
               + 0.72*rel5_term + 0.5*rel20_term + ema_week - 0.35*atr_penalty
```
Post-score penalties (`sector_audit.py:1947-1959`): `trap_exit_proxy` → day −8/week −5;
high-volume-down-day stress → day −6/week −4; `event_risk_soon` → day −4/week −6. Both clamped to
[0,100]. `_ema_history_confidence`: `0.35` if rows<30, else `min(1.0, max(0.35, rows/200))`.

---

## 3. Sector ranking formula (`rank_score`)

File: `src/sector_priority.py`. The ranking score is produced by `_score_from_features`
(`:2114-2135`) and then combined with a sector-level news blend inside `build_sector_rankings`
(`:2227-2235`).

### 3.1 The core `rank_score`

`_score_from_features` (`sector_priority.py:2114-2135`):
```
ret_1w_term     = 1.1 * (percentile_1w / 100) * TITAN_RANK_PCTILE_1W_REF   (default ref 11.0)
ret_1m_term     = 0.45 * (percentile_1m / 100) * TITAN_RANK_PCTILE_1M_REF  (default ref 11.0)
absorption_term = 8.0 * (absorption - 1.0)   (0 if NaN)
base_score      = cap_bias + ret_1w_term + ret_1m_term + absorption_term
rank_score_technical = round(base_score - overextension_penalty, 4)
```
Percentiles are computed cross-sectionally per sector cohort via `percentile_rank_0_100`
(`tape_metrics.py`). Missing 1w/1m history defaults to the sector-median percentile (50.0).
At p100 with default refs, the momentum terms approximate the legacy raw-return scale
(e.g. p100 1w → ~12.1 pts, same as an 11% weekly return × 1.1).

i.e.

```
rank_score = cap_bias
           + 1.10 * (percentile_1w/100) * PCTILE_1W_REF
           + 0.45 * (percentile_1m/100) * PCTILE_1M_REF
           + 8.0  * (absorption - 1.0)
           - overextension_penalty            # §3.2
           + news_blend_points                # §3.3 (added in build_sector_rankings)
           + v2_rank_adjustment               # §3.1a (signal_v2 risk_net reconciliation)
           × gate_multiplier                  # other shadow gates (v2 gate no longer multiplies)
```

### 3.1a `v2_rank_adjustment` (signal_v2 reconciliation)

Latest stored `action_signal` + `signal_reason_trace.risk_net` (fallback label→risk map)
from `symbol_daily_features` are translated into a bounded bonus/penalty before other gates:

- `risk_net < 5` (buy/accumulate/hold): bonus up to **+1.5** (`TITAN_V2_RANK_BONUS_MAX`), linear
  from trim threshold down to 0.
- `risk_net ≥ 5` (trim/exit-risk): penalty up to **−6.0** (`TITAN_V2_RANK_PENALTY_MAX`), linear
  from 5→10 on the 0–10 `risk_net` scale. When sector overextension penalty already fired,
  v2 penalty is damped by `TITAN_V2_RANK_VOL_DAMP_FRAC` (default 0.25) to avoid double-counting
  volatility/stretch already in `risk_c`.

The legacy `v2_risk_label` gate records posture but sets `score_multiplier = 1.0` (no double
penalty). In `skip` mode it may still **withhold** only extreme exit-risk (`risk_net ≥ 7`).
Meta exposes `v2_rank_adjustment` and `dual_engine_conflict` when a top-quartile momentum name
still carries trim/exit-risk.

**`cap_bias`** rewards smaller caps (the stated objective is higher-move small/micro names)
(`_cap_bias`, `sector_priority.py:1733-1742`; buckets `_bucket_from_market_cap_cr`, `:1721-1730`):

| Market-cap bucket | Threshold (INR crore) | `cap_bias` |
|---|---|---|
| micro | `< 5,000` | 8.0 |
| small | `5,000 – 20,000` | 6.0 |
| mid | `20,000 – 50,000` | 3.0 |
| large | `≥ 50,000` | 1.0 |
| unknown | (cap missing) | 0.0 |

- `absorption` here is the volume-participation ratio (`volume_participation_ratio(df)`,
  `sector_priority.py:2192`); `absorption = 1.0` is neutral, so `8*(absorption−1)` is +8 per unit
  of above-average participation and negative for low participation.
- `return_1w` / `return_1m` are 5-/20-session % returns (`_return_pct`, `:1973-1981`).

### 3.2 Overextension penalty (Fix A)

`_overextension_penalty` (`sector_priority.py:2028-2084`), gated on by `TITAN_OVEREXT_ENABLED`
(default on, `:1994-1998`). Two smooth ramps, summed and capped. `_ramp(value, zero, full, pts)`
is a clamped linear 0→pts ramp (`:2020-2025`). Constants (`:2005-2017`):

```
# stretch channel (ATR-normalized EMA200 stretch)
stretch_pen = _ramp(stretch, zero_at=3.0, full_at=7.0, full_points=9.0) * run_gate
# where run_gate scales the stretch channel down if the name has not actually run up:
run_ctx  = max(return_1w, return_1m)          # NaN-safe
run_gate = _ramp(run_ctx, zero_at=0.0, full_at=4.0, full_points=1.0)   # 1.0 if no recent run data

# 1-week run channel, amplified by volume participation/absorption
run_base = _ramp(return_1w, zero_at=6.0, full_at=12.0, full_points=6.0)
amp      = 1.0 + 0.25 * clamp(absorption - 1.0, 0.0, 2.0)     # absorption amplifier
run_pen  = run_base * amp

penalty  = clamp(stretch_pen + run_pen, 0.0, 18.0)            # absolute cap 18.0
```

| Constant | Default | Env knob |
|---|---|---|
| stretch deadband | 3.0 ATR | `TITAN_OVEREXT_STRETCH_DEADBAND` |
| stretch full | 7.0 ATR | `TITAN_OVEREXT_STRETCH_FULL` |
| stretch weight (max points) | 9.0 | `TITAN_OVEREXT_STRETCH_WEIGHT` |
| run deadband | 6.0% (1w) | `TITAN_OVEREXT_RUN_DEADBAND_PCT` |
| run full | 12.0% (1w) | `TITAN_OVEREXT_RUN_FULL_PCT` |
| run weight (max points) | 6.0 | `TITAN_OVEREXT_RUN_WEIGHT` |
| absorption amplifier | 0.25 | `TITAN_OVEREXT_ABSORPTION_AMP` |
| penalty cap | 18.0 | `TITAN_OVEREXT_PENALTY_CAP` |
| run-gate zero / full | 0.0% / 4.0% | `TITAN_OVEREXT_RUN_GATE_ZERO_PCT` / `_FULL_PCT` |

Intent (per code comments `:2001-2017`): demote volatility-stretched and/or climactically run-up
winners, while leaving orderly risers nearly untouched. **Caveat:** `stretch` requires full
history + OHLC for ATR; the ranking module's short 90-day live fetch (`build_sector_rankings`,
`:2176-2196`) often yields `stretch = NaN`, so in practice the **run channel usually does all the
work** and the stretch channel is dormant (`_stretch_inputs_from_df`, `:2087-2111`).

### 3.3 News blend

Sector-level (theme) news sentiment is converted to ranking points and added to every stock in
the sector (`build_sector_rankings`, `:2150-2235`).

`_news_blend_points(sector_news_score)` (`sector_priority.py:1558-1560`):
```
points = sector_news_score * news_blend_weight        # weight default 3.5
points = clamp(points, -news_blend_cap, +news_blend_cap)   # cap default 3.0
```

| Knob | Default | Env |
|---|---|---|
| blend weight | 3.5 | `TITAN_NEWS_BLEND_WEIGHT` |
| blend cap | ±3.0 | `TITAN_NEWS_BLEND_CAP` |

`sector_news_score ∈ [−1,+1]` is the normalized per-theme score from `score_sector_news`
(`:1289-1348`):
```
per item: contribution = theme_weight * sentiment * impact * confidence
          abs_weight    = theme_weight * impact
score = clamp( Σ contribution / Σ abs_weight , -1, 1 )       # 0 if no weight
```
where (`:1224-1286`):
- `sentiment = (pos_hits − neg_hits) / max(2, pos_hits+neg_hits+1)`, clamped [−1,1], from fixed
  positive/negative term sets (`_POSITIVE_NEWS_TERMS`/`_NEGATIVE_NEWS_TERMS`, `:102-125`).
- `impact = clamp(0.25 + Σ matched-impact-term deltas (+0.1 if len>180), 0.05, 1.0)` from
  `_IMPACT_NEWS_TERMS` (e.g. `sanction 0.45`, `bulk deal 0.4`, `tariff 0.35`, …, `:126-151`).
- `confidence = clamp(0.45 + 0.15·has_source + 0.10·has_url + 0.15·(reputable wire), 0.2, 1.0)`
  (`_news_confidence_score`, `:1267-1276`).
- `theme_weight = min(2.0, 1.0 + (hits−1)·0.25)` from sector keyword hits (`_theme_hits_for_sector`,
  `:1279-1286`); themes defined in `_SECTOR_THEME_KEYWORDS` (`:51-101`): ai, defence, data_centre,
  electronics_ems, renewables_clean_energy, railways_transport_infra.

There is also a **stock-level** news path (`correlate_stock_news_with_macro`, `:1435-1555`) that
blends `stock_score*0.7 + macro_score*0.3` for explainability/digest, but the **ranking score uses
only the sector (theme) blend** above.

### 3.4 Winners selection & persistence

`build_sector_rankings` (`:2284-2302`): sort by `(rank_score, return_1w_pct)` descending; among
names with price history, **prefer micro/small-cap** for the top-N "priority" set
(`preferred = micro/small`, then the rest as fallback), tag `is_priority`, assign
`rank_in_sector`. Persistence:
- `persist_sector_rankings` → table `sector_priority_rankings` (`:2305-2327`).
- `persist_daily_winners` → table `sector_daily_winners` with a `score_breakdown`
  (technical_rank_score, overextension_penalty/components, news sector score & blend points)
  (`:2440-2521`).
- `load_priority_instruments` reads today's IST `as_of_date`, falling back to the latest available
  date (`:2385-2437`).

---

## 4. The waterfall / layered signal logic (`src/signal_v2.py`)

`evaluate_signal_v2(audit)` (`signal_v2.py:723-788`) runs the layers in call order **A → C → D →
B**, then aggregates and maps in **E**. It returns `(label, round(risk_net,2), reasons[:8])` and
writes `signal_confidence`, `signal_reason_trace`, `signal_engine_version="v2"` onto the audit.

Shared helpers: `_sf` float-coerce→NaN (`:86-91`), `_clamp` (`:141-142`),
`_ramp(value, zero_at, full_at, full_points)` clamped linear ramp, NaN→0 (`:145-154`).

`CORE_METRICS` (NaN census set, `:66-75`): `z_score, cmf_20, adx_14, ema_200_distance_pct,
atr_14_pct, return_1d_pct, return_5d_pct, return_10d_pct`.
`_SEVERITY` (`:77-83`): `buy 0 < accumulate 1 < hold 2 < trim 3 < exit-risk 4`.

### Layer A — data-quality / sanity (`layer_a`, `:166-206`)
Can only **withhold buy**, **cap the label**, or **shave confidence** — never asserts buy.
Defaults: `TITAN_SIGV2_A_NAN_MAX = 3` (`:178`), `TITAN_SIGV2_A_SHORT_HISTORY_CONF = 0.6` (`:179`).
```
nan_count = #{m in CORE_METRICS : isnan(audit[m])}
if nan_count > 0:   seed *= max(0, 1 - 0.05*nan_count)        # each NaN shaves 5%
if nan_count >= 3:  buy_allowed=False; seed *= 0.5
if history_lt_200_sessions: buy_allowed=False; label_ceiling="hold"; seed *= 0.6
if liquidity_thin_proxy:    buy_allowed=False
confidence_seed = clamp(seed, 0, 1)
```
`label_ceiling="hold"` later caps **constructive** labels only (never blocks downgrades).

### Layer C — graded evidence (`layer_c`, `:285-395`; families `_family_points`, `:214-282`)
Legacy discrete families re-expressed as **linear ramps**; a term is traced only if points `>0.05`.
Layer-D multipliers are **not** applied here (applied in E) so raw terms stay inspectable.

**Risk families** (`_family_points`, all bearish, each capped):

| Family | Metric | `_ramp(zero → full, full_points)` | Cap | Line |
|---|---|---|---|---|
| horizon | `next_week_score` | 55 → 45, 3.0 | 3.0 | `:245-247` |
| intent | `effective_intent_score` | 52 → 45, 2.0 | 2.0 | `:250-252` |
| z (downside only) | `z_score` | −1 → −2, 2.0 | 2.0 | `:255-257` |
| momentum | `return_1d_pct` | −1 → −2, `2.0*ret1d_weight` | (sum capped 3.0) | `:261` |
| momentum | `return_5d_pct` | −2 → −6, 2.0 | | `:262` |
| momentum | `return_10d_pct` | −6 → −10, 1.5 | | `:263` |
| trend (below only) | `ema_200_distance_pct` | −2 → −6, 2.0 | 2.0 | `:268` |
| volatility (preferred) | `atr_penalty_input` | 1.25 → 2.2, 2.0 | 2.0 | `:274` |
| volatility (fallback) | `atr_14_pct` | 4.0 → 6.0, 2.0 | 2.0 | `:277` |

Volatility family is **suppressed** when `adx ≥ 25 AND +DI > -DI` (strong bullish trend) to
avoid double-count with stretch/over-extension. In that regime C-8 stretch deadband is widened
×1.5 (`TITAN_SIGV2_C_STRETCH_DEADBAND_BULL_MULT`).

`ret1d_weight = 0.45 if extreme_price_move_proxy else 1.0` (`:229`). Momentum sub-terms are summed
then `min(3.0, …)` (`:264`).

**C-7 money flow** (`:315-341`) — dead-band ±0.05, scaled by magnitude. Defaults
`TITAN_SIGV2_C_CMF_K = 10.0` (`:301`), `TITAN_SIGV2_C_CMF_CAP = 2.0` (`:302`):
```
if cmf < -0.05:  money_flow_bear = clamp((-0.05 - cmf)*K, 0, cap);  if NOT obv_trend_confirm: *=1.25
elif cmf > 0.05: money_flow_bull = clamp((cmf - 0.05)*K, 0, cap);   if obv_trend_confirm: *=1.25
# |cmf| <= 0.05 -> dead-band, both 0
```
`obv_trend_confirm = (obv_latest > obv_ema_20)` computed in the audit pipeline
(`titan_engine.calculate_obv_trend_confirm`, persisted as `obv_latest`, `obv_ema_20`,
`obv_trend_confirm`). Replaces the prior absolute-sign OBV slope rule.
The ×1.25 OBV rule only **amplifies an already-nonzero same-sign** term. `bull_terms =
money_flow_bull`.

**C-8 ATR-normalized over-extension** (`:343-355`) — upside only. Defaults
`TITAN_SIGV2_C_STRETCH_DEADBAND_ATR = 3.0` (`:305`, *lowered from 4.0 by Fix A*),
`TITAN_SIGV2_C_STRETCH_RAMP_ATR = 8.0` (`:306`), `TITAN_SIGV2_C_STRETCH_CAP = 2.0` (`:307`):
```
over_ext = _ramp(ema200_stretch_atr, zero_at=3.0, full_at=8.0, full_points=2.0)
over_extension_hot = (stretch not NaN) and stretch >= 3.0
if over_ext>0 and sector_pctile_ema200_stretch >= 90: over_ext *= 1.25   # top-decile corroborator
```
`over_extension_hot` is a key flag consumed by Layers B, D and the BUY gate.

**C-8b upside-z over-extension** (`:357-365`) — symmetric to the downside-z bear term. Defaults
`TITAN_SIGV2_C_UPSIDE_Z_DEADBAND = 2.5` (`:311`), `_UPSIDE_Z_RAMP = 4.0` (`:312`),
`_UPSIDE_Z_CAP = 1.5` (`:313`):
```
upside_z = _ramp(z_score, zero_at=2.5, full_at=4.0, full_points=1.5)   # a stretched-UP move adds risk
```

**Fundamentals** (`:367-383`): `fundamental_status` → `weak +2.0`, `balanced +1.0`, `strong −1.0`,
else 0 (can reduce risk).

### Layer D — context modifiers (`layer_d`, `:403-475`)
Produces multipliers/bumps/flags; **never returns a label**. Defaults: `TITAN_SIGV2_D_ADX_WEAK =
20.0` (`:425`), `_ADX_STRONG = 25.0` (`:426`), `_DIVERGENCE_RET1D = 2.0` (`:427`),
`_PULLBACK_VPR = 1.0` (`:428`), `_STALEFLOW_OBV_EPS = 0.0` (`:429`). `vpr` reads
`volume_participation_ratio` (fallback `absorption_ratio`, `:423`).

1. **Directional ADX regime** (`:433-543`): `adx < 20` → `mult_money_flow=1.3,
   mult_over_extension=1.3, mult_momentum=0.7` (mean-reversion). `adx ≥ 25 AND +DI > -DI` →
   `mult_momentum=1.3, mult_risk=0.8` (volatility term in aggregate scaled ×0.8). `adx ≥ 25 AND
   -DI > +DI` → `mult_momentum=0.5, mult_risk=1.5`. `20 ≤ adx < 25` deadband persists prior
   session multipliers from `prev_adx_regime_mults` / `tape_extras.adx_regime_mults` (default 1.0).
2. **Money-flow divergence ("hollow breakout")** (`:446-449`): `ret1d > 2.0 AND cmf < -0.05` →
   `divergence_bump = +1.0` (added to risk) and `buy_confidence_cap = 0.5`.
3. **Healthy-pullback rescue** (`:452-461`): `ret1d < 0 AND vpr < 1.0 AND cmf > 0.05 AND ret5d ≥
   -3.0 AND ema_200_distance_pct ≥ 0` → `mult_momentum = min(mult_momentum, 0.5)` and
   `pullback_bull_bump = +0.5`.
4. **Stale-flow OBV tiebreaker (GREAVESCOT rule)** (`:465-472`): `-0.05 ≤ cmf ≤ 0.05 (neutral) AND
   over_extension_hot AND adx < 20 AND obv_trend_confirm is not True` → `staleflow_downgrade = True`
   (forces TRIM in B and counts as a Tier-2 corroborator).

### Layer B — two-tier hard disqualifiers (`layer_b`, `:493-569`)
Runs after C and D (needs `over_extension_hot` and `staleflow_downgrade`). Defaults:
`TITAN_SIGV2_B_TIER1_GAP_PCT = -8.0` (`:503`), `_B_TIER2_TRIM_COUNT = 2` (`:504`),
`_B_TIER2_EXIT_COUNT = 3` (`:505`).

**Tier-1 instant-exit whitelist** (single signal → `forced_label="exit-risk"`,
`bypass_hysteresis=True`; short-circuits B, `:510-527`):
```
t1_structural = structural_break_proxy AND (ret1d <= -8.0 OR gap_down_proxy)
t1_liquidity  = liquidity_thin_proxy AND (0 < median_notional_inr_20d < liquidity_floor)
```
`liquidity_floor = TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR` (default ₹1,200,000;
`_liquidity_floor_inr`, `:483-490`).

**Tier-2 corroboration counting** (`:529-568`) — collect distinct bearish signals:
- `vpr-proxy stress` if any of `trap_exit_proxy / high_volume_down_day_proxy /
  panic_absorption_proxy` — **counts once** (all derive from the same VPR).
- `cmf distribution` if `cmf < -0.05`.
- `over-extension hot` if `over_extension_hot`.
- `weak ADX with -DI dominance` if `adx < 20 AND minus_di > plus_di`.
- `event risk` if `event_risk_soon OR event_guardrail_applied`.
- `stale-flow downgrade` if Layer-D `staleflow_downgrade`.
- `into call OI wall` / `below put OI support` — options corroborators (§ below).
```
count = #signals
if count >= 3:                          forced_label = "exit-risk"
elif count >= 2 or staleflow_downgrade: forced_label = "trim"
```
`corroborators = count` (feeds the confidence formula).

**Options corroborators** (`_options_into_call_wall` `:100-121`, `_options_below_put_support`
`:124-138`): only when an option chain is available (F&O names). "Into call wall" = spot within ±1%
of the max-call-OI strike with bearish context (sell∈{trim,exit-risk} OR cmf<−0.05 OR ret1d<0 OR
z<0). "Below put support" = spot below max-put-OI strike with distribution/negative tape.

### Layer E — aggregation, mapping, confidence, hysteresis (`:577-788`)

**Aggregate** `_aggregate(c, d)` (`:577-601`) — apply D multipliers/bumps to C terms:
```
risk_c  = horizon + intent + z
        + min(3.0, momentum * mult_momentum)        # momentum re-capped at 3
        + trend + volatility * mult_risk
        + money_flow_bear * mult_money_flow
        + over_extension  * mult_over_extension
        + upside_z        * mult_over_extension     # C-8b uses the over-extension multiplier
        + fundamental                               # may be negative
        + divergence_bump
risk_c  = clamp(risk_c, 0, 10)

bull_c  = money_flow_bull * mult_money_flow + pullback_bull_bump
bull_c  = clamp(bull_c, 0, 10)
```

**Net risk** (`:740`), `TITAN_SIGV2_E_BULL_OFFSET = 0.5` (`:737`):
```
risk_net = clamp(risk_c - 0.5 * bull_c, 0, 10)
```
Bull evidence offsets bear at **half weight** — it can pull a name down the risk ladder but never
directly forces a buy (the BUY gate is independent).

**BUY gate** `_buy_gate(audit, a, c)` (`:604-632`) — all thresholds are in-code constants:
```
core = buy_allowed AND not trap_exit_proxy AND not liquidity_thin_proxy
       AND not extreme_price_move_proxy
       AND next_week_score >= 70 AND effective_intent_score >= 65
       AND not (return_5d_pct <= -4) AND not (rel_return_5d_vs_nifty_pct <= -3)
flow_ok          = isnan(cmf) OR cmf >= -0.05
not_overextended = not over_extension_hot
clean_buy         = core AND flow_ok AND not_overextended
constructive_core = core
```

---

## 5. Label / ceiling decision logic

**Mapping ladder** `_map_label(risk_net, gate, a)` (`:635-647`):
```
risk_net >= 7:                       exit-risk
risk_net >= 5:                       trim          (TITAN_SIGV2_E_TRIM_RISK_MIN)
risk_net <  5 and clean_buy and risk < 3:         buy   (TITAN_SIGV2_E_BUY_RISK_MAX)
risk_net <  5 and constructive_core and risk < 3: accumulate
otherwise:                           hold
```

Then applied in order inside `evaluate_signal_v2` (`:743-752`):
1. **`_apply_ceiling`** (`:650-654`): if `label_ceiling == "hold"` and label ∈ {buy, accumulate} →
   `hold`. (Caps the constructive side only — e.g. `history_lt_200_sessions` forces ceiling=hold.)
2. **`_escalate`** (`:657-660`): Layer-B `forced_label` overrides **only if strictly more severe**
   per `_SEVERITY` — bearish disqualifiers win ties; constructive labels can never be upgraded.
3. **`_apply_hysteresis`** (`:663-694`, `TITAN_SIGV2_E_HYST_BUFFER = 0.5`, `:738`): asymmetric
   deadbands — enter buy/accumulate only when `risk_net < 3.0`; enter trim when
   `risk_net ≥ 5.0`; from trim/exit-risk cannot upgrade to constructive until
   `risk_net < 3.0` (with recovery tape when wired). Danger transitions and Tier-1 `bypass`
   apply same-day.

> **Note:** `accumulate` is a **first-class always-on label** in the current code (module
> docstring `:1-17`); there is no accumulate-disable gate in `evaluate_signal_v2`.

**Confidence** `_confidence(...)` (`:697-720`):
```
if bypass:                         return clamp(max(0.9, seed), 0, 1)
dominant_bear = label in {trim, exit-risk} or risk_net >= 4
if dominant_bear: n = #bear trace terms (points>0.05) + corroborators
else:             n = #bull trace terms (points>0.05)
base = clamp(0.4 + 0.1*n, 0, 0.95)
conf = base * confidence_seed
if label in {buy, accumulate} and buy_confidence_cap is not None:  conf = min(conf, buy_confidence_cap)
return round(clamp(conf, 0, 1), 3)
```

**Plain-English label meanings** (`action_signals.action_signal_plain_english`, `:57-73`):
buy = constructive setup; accumulate = constructive but extended (add on pullbacks); hold = risk
<4; trim = risk 4–6; exit-risk = risk ≥7 (hard exit bar).

---

## 6. Thresholds & env knobs table

### 6.1 Signal engine (`src/signal_v2.py`)

| Key | Default | Meaning | Line |
|---|---|---|---|
| `TITAN_SIGV2_A_NAN_MAX` | 3 | NaN census that withholds buy | `:178` |
| `TITAN_SIGV2_A_SHORT_HISTORY_CONF` | 0.6 | confidence ×factor for short history | `:179` |
| `TITAN_SIGV2_C_CMF_K` | 10.0 | CMF→points scale | `:301` |
| `TITAN_SIGV2_C_CMF_CAP` | 2.0 | money-flow term cap | `:302` |
| `TITAN_SIGV2_C_STRETCH_DEADBAND_ATR` | **3.0** | C-8 over-extension deadband | `:305` |
| `TITAN_SIGV2_C_STRETCH_RAMP_ATR` | 8.0 | C-8 ramp full point | `:306` |
| `TITAN_SIGV2_C_STRETCH_CAP` | 2.0 | C-8 cap | `:307` |
| `TITAN_SIGV2_C_UPSIDE_Z_DEADBAND` | 2.5 | C-8b upside-z deadband | `:311` |
| `TITAN_SIGV2_C_UPSIDE_Z_RAMP` | 4.0 | C-8b ramp full point | `:312` |
| `TITAN_SIGV2_C_UPSIDE_Z_CAP` | 1.5 | C-8b cap | `:313` |
| `TITAN_SIGV2_D_ADX_WEAK` | 20.0 | weak-trend threshold | `:425` |
| `TITAN_SIGV2_D_ADX_STRONG` | 25.0 | strong-trend threshold | `:426` |
| `TITAN_SIGV2_D_DIVERGENCE_RET1D` | 2.0 | hollow-breakout ret1d gate | `:427` |
| `TITAN_SIGV2_D_PULLBACK_VPR` | 1.0 | pullback VPR ceiling | `:428` |
| `TITAN_SIGV2_D_STALEFLOW_OBV_EPS` | 0.0 | stale-flow OBV epsilon | `:429` |
| `TITAN_SIGV2_B_TIER1_GAP_PCT` | −8.0 | Tier-1 severe-down gate | `:503` |
| `TITAN_SIGV2_B_TIER2_TRIM_COUNT` | 2 | corroborators → trim | `:504` |
| `TITAN_SIGV2_B_TIER2_EXIT_COUNT` | 3 | corroborators → exit-risk | `:505` |
| `TITAN_SIGV2_E_BULL_OFFSET` | 0.5 | bull offset weight in risk_net | `:737` |
| `TITAN_SIGV2_E_BUY_RISK_MAX` | 3.0 | buy/accumulate ceiling | `:57` |
| `TITAN_SIGV2_E_TRIM_RISK_MIN` | 5.0 | trim entry floor | `:58` |
| `TITAN_SIGV2_E_HYST_BUFFER` | 0.5 | hysteresis buffer around deadbands | `:738` |
| `TITAN_RANK_PCTILE_1W_REF` | 11.0 | percentile→points scale (1w) | `sector_priority.py` |
| `TITAN_RANK_PCTILE_1M_REF` | 11.0 | percentile→points scale (1m) | `sector_priority.py` |
| `TITAN_V2_RANK_TRIM_THRESHOLD` | 5.0 | v2 rank bonus/penalty pivot | `sector_priority.py` |
| `TITAN_EMA200_LOOKBACK_CALENDAR_DAYS` | 400 | min fetch for EMA200 (~250 sessions) | `sector_audit.py` |
| `TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR` | 1,200,000 | liquidity floor (₹) | `:483-490` |

In-code constants (not env-tunable): BUY gate `next_week_score ≥ 70`, `effective_intent_score ≥
65`, `return_5d_pct > −4`, `rel_return_5d ≥ −3` (`:617-628`); label ladder edges `7.0`/`4.0`
(`:637-639`); confidence floor `0.9` on bypass and `0.4 + 0.1n` base (`:709-716`).

### 6.2 Sector ranking / news / overextension (`src/sector_priority.py`)

| Key | Default | Meaning | Line |
|---|---|---|---|
| `TITAN_OVEREXT_ENABLED` | on | enable ranking overextension penalty | `:1994-1998` |
| `TITAN_OVEREXT_STRETCH_DEADBAND` | 3.0 | stretch channel deadband | `:2005,2048` |
| `TITAN_OVEREXT_STRETCH_FULL` | 7.0 | stretch channel full | `:2006,2049` |
| `TITAN_OVEREXT_STRETCH_WEIGHT` | 9.0 | stretch channel max points | `:2007,2050` |
| `TITAN_OVEREXT_RUN_DEADBAND_PCT` | 6.0 | run channel deadband (1w%) | `:2008,2051` |
| `TITAN_OVEREXT_RUN_FULL_PCT` | 12.0 | run channel full (1w%) | `:2009,2052` |
| `TITAN_OVEREXT_RUN_WEIGHT` | 6.0 | run channel max points | `:2010,2053` |
| `TITAN_OVEREXT_ABSORPTION_AMP` | 0.25 | run-channel volume amplifier | `:2011,2054` |
| `TITAN_OVEREXT_PENALTY_CAP` | 18.0 | total penalty cap | `:2012,2055` |
| `TITAN_OVEREXT_RUN_GATE_ZERO_PCT` | 0.0 | stretch run-gate zero | `:2016,2056` |
| `TITAN_OVEREXT_RUN_GATE_FULL_PCT` | 4.0 | stretch run-gate full | `:2017,2057` |
| `TITAN_NEWS_BLEND_WEIGHT` | 3.5 | sector news → rank points | `:47,324-331` |
| `TITAN_NEWS_BLEND_CAP` | ±3.0 | news blend cap | `:48,334-341` |
| `TITAN_NEWS_FETCH_LIMIT` | 40 | global news items | `:45,277-284` |
| `TITAN_STOCK_NEWS_FETCH_LIMIT` | 8 | per-stock news items | `:46,287-294` |
| `TITAN_NEWS_MAX_AGE_HOURS` | 36 | news staleness cutoff | `:43,297-304` |
| `TITAN_NEWS_SNAPSHOT_TTL_HOURS` | 2.0 | snapshot reuse TTL | `:44,307-314` |
| `TITAN_NEWS_DRIVER_LIMIT` | 3 | top drivers kept | `:49,344-351` |
| `TITAN_STOCK_NEWS_MIN_RELEVANCE` | 0.35 | stock-news relevance floor | `:243,385-392` |
| `TITAN_SENTIMENT_MODEL` | vader | vader / finbert | `news_sentiment.py:37-41` |

`cap_bias` thresholds (`_bucket_from_market_cap_cr`, `:1721-1730`): micro <5,000cr, small
<20,000cr, mid <50,000cr, else large — all in-code constants.

### 6.3 Macro / other

| Key | Default | Meaning | File:line |
|---|---|---|---|
| `TITAN_PM_LIVE_FETCH` | on | live precious-metals macro fetch | `pm_macro_data.py:31-34` |
| `TITAN_PM_MACRO_CSV` | data/cache/pm_macro_series.csv | macro cache path | `pm_macro_data.py:220` |
| `TITAN_RECONCILE_MODE` / `_REPORT_ONLY` | (set by runner) | block market fetch during reconcile | `reconcile_runner.py:28-42` |

---

## 7. Reconciliation / hit-rate

Two related mechanisms:

**(a) Production EOD reconcile** — `reconcile_runner.run_reconcile_report` (`:51-113`) runs in a
guarded "reconcile mode" (Breeze/market fetch blocked) and calls into `analysis_store`
(`build_stock_reconcile_snapshot`, `build_reconcile_digest_lines`) to compute decision-efficacy
report lines from persisted analytics tables, then emails a report. Core direction logic
(`analysis_store.py`):
```
_return_direction(r): up if r>=0.3 ; down if r<=-0.3 ; neutral otherwise (0.3% dead band)   (:997-1005)
_direction_hit(pred, realized):                                                              (:1008-1013)
    None if either unknown ; if pred==neutral -> realized==neutral ; else pred==realized
_transition_quality(prev, curr): improving if Δ>=3 ; deteriorating if Δ<=-3 ; else stable    (:1016-1026)
```

**(b) Offline A/B harness** — `signal_v2_backtest.py` replays `symbol_daily_features` rows,
rebuilds audits (`feature_row_to_audit`, `:171-204`), recomputes labels via `evaluate_signal_v2`
(v2) vs `_derive_action_signal_legacy` (reference), and computes spec metrics
(`run_legacy_vs_v2_ab`, `:500-582`):
- **Predicted direction** from label: buy/accumulate→up, trim/exit-risk→down, else neutral
  (`signal_predicted_direction`, `:107-114`).
- **direction_hit_rate** = hits / total over rows where direction is known.
- **defensive_escalation_events** + **drawdown_saved_mean_pct** (`−return_1d_pct` when v2 is more
  defensive than legacy on a name that then fell) (`:121-140, 359-364`).
- **false_exit_rescue_events** + **false_exit_forgone_mean_pct** (opportunity cost when v2 stays
  long vs legacy trim/exit) (`:128-148, 365-369`).
- **flip_rate** per symbol = label changes / (n−1) (`:151-155`); a flip guardrail checks v2
  flip-rate ≤ legacy + 0.05 (`:555-557`).
- **confidence calibration** buckets: low (<0.55) vs high (≥0.75) hit-rate (`:378-384`).

Walk-forward labels feed `prev_action_signal` so v2 hysteresis is exercised in backtest only
(`walk_labels`, `:270-294`).

---

## 8. Known caveats & code-vs-doc drift

Detailed bug tracking lives elsewhere; this is a short reviewer-facing list of methodology
caveats and stale-doc points found by diffing code against the in-repo docs.

1. **`docs/signal_v2_metrics_and_waterfall.md` is stale on three points** (code is ground truth):
   - It lists `TITAN_SIGV2_C_STRETCH_DEADBAND_ATR = 4.0`; the code default is now **3.0** (Fix A
     lowered it, `signal_v2.py:303-305`). This makes C-8 over-extension and `over_extension_hot`
     more sensitive than the doc implies.
   - It omits the **C-8b upside-z over-extension** term entirely (`signal_v2.py:308-313, 357-365`,
     and `upside_z * mult_over_extension` in `_aggregate`, `:593`). A statistically stretched *up*
     move now adds mean-reversion risk symmetric to the downside-z term.
   - It describes an **"accumulate gating"** (`TITAN_SIGV2_ENABLE_ACCUMULATE`, default off) that
     collapses accumulate→hold. The current code has **no such gate**; `accumulate` is a
     first-class always-on label. Its line-number citations are also offset (~25 lines) from the
     current file.

2. **Ranking overextension stretch channel is usually dormant.** `_score_from_features` accepts a
   `stretch` input, but `build_sector_rankings` fetches only ~90 calendar days
   (`sector_priority.py:2176-2196`), which is too short for EMA200/ATR, so `stretch = NaN` and only
   the **1-week run channel** drives the penalty (`_stretch_inputs_from_df`, `:2087-2111`).

3. **Hysteresis is defined but inert in production.** `prev_action_signal` is only populated by the
   backtest harness (`signal_v2_backtest.py:239`); live runs always hit the `prior is None → no-op`
   branch (`signal_v2.py:677, 747-752`). Stated 2-session stickiness does not apply to live labels.

4. **ADX/ATR use rolling-sum/SMA, not classic Wilder smoothing** (`titan_engine.py:60-99, 38-57`).
   Values are close to but not identical to standard TA-Lib ADX/ATR; thresholds (ADX 20/25, ATR%
   ramps) are calibrated to this approximation.

5. **Two separate "absorption/participation" notions.** The ranking module's `absorption`
   (`8*(absorption−1)`) and the signal engine's `volume_participation_ratio`/`absorption_ratio`
   are the same 5-session VPR concept but enter different scores; true delivery-based "absorption"
   and FII/DII institutional flow are **not wired** (`institutional_flow.available = False`,
   `sector_audit.py:3187-3198`).

6. **News sentiment is heuristic by default.** The ranking news blend uses keyword term-set
   sentiment (`sector_priority.py:1224-1236`), not the VADER/FinBERT models in
   `news_sentiment.py`; the latter route is selectable via `TITAN_SENTIMENT_MODEL` but is used on
   the per-item analysis path, not the sector ranking blend.

7. **Live external dependencies.** Market cap (NSE→Moneycontrol→Screener→Yahoo cascade,
   `sector_priority.py:2197-2208`), news RSS, and yfinance macro are best-effort with graceful
   `NaN`/fallback handling; missing data degrades scores rather than failing, which a reviewer
   should keep in mind when interpreting any single run.
