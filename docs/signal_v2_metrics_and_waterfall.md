# Titan v2 Signal Engine — Metric Formulas & Waterfall Reference

This document is a **self-contained validation reference** for the v2 signal
engine (`src/signal_v2.py`). It is written so an external reviewer with **no access to the
codebase** can re-derive every number and confirm the logic. Every formula and rule is
cited as `file:line`.

**Scope / data conventions used throughout**
- All price/volume metrics are computed on a trailing daily OHLCV `DataFrame` (`df`), newest
  row last. No look-ahead: every metric uses only data up to and including the latest row.
- "Returns" are close-to-close percentage returns in **percent units** (e.g. `-2.0` = −2%).
- `NaN` is the explicit "unavailable" sentinel. Helpers coerce non-numerics to `NaN`.
- The audit dict is the single shared payload. Metrics (Part 1) are written onto it by
  `src/sector_audit.py`; the v2 engine (Part 2) consumes it.
- Engine entry point: `action_signals.derive_action_signal` always calls
  `evaluate_signal_v2(audit)` (`src/action_signals.py`, `src/signal_v2.py`).

**Shared numeric helpers (v2 engine)**
- `_sf(v)` — float-coerce; any `TypeError`/`ValueError`/`NaN` → `NaN` (`src/signal_v2.py:97-102`).
- `_clamp(x, lo, hi) = max(lo, min(hi, x))` (`src/signal_v2.py:105-106`).
- `_ramp(value, zero_at, full_at, full_points)` — linear ramp, direction-agnostic, clamped to
  `[0, full_points]`; `NaN` input or `zero_at == full_at` → `0.0`
  (`src/signal_v2.py:109-118`):

```
if isnan(value) or zero_at == full_at: return 0
frac = (value - zero_at) / (full_at - zero_at)
return clamp(frac, 0, 1) * full_points
```

- `_safe_float(x)` in sector_audit mirrors `_sf` (`src/sector_audit.py:1017-1021`).
- Config readers (defaults live in code): `_env_truthy` (`:32-36`), `_env_float` (`:39-46`),
  `_env_int` (`:49-56`). Empty/invalid env values fall back to the in-code default.

---

# PART 1 — Metric formulas

## 1.1 z_score (blended) + z_score_fast_20 + z_score_slow

**Base rolling z-score** — `calculate_z_score(data, window=20)`
(`src/titan_engine.py:12-25`):
```
s    = numeric(close).dropna()
win  = min(window, len(s))
tail = s[-win:]
mu   = mean(tail);  sigma = std(tail, ddof=0)   # population std
last = tail[-1]
if s.empty or window < 2:        return NaN
if sigma == 0 or isnan(sigma):   return 0.0
z = (last - mu) / sigma
```
- Inputs: close series. Window: 20 (default). Units: standard deviations (dimensionless).
- Range: unbounded, typically ≈ [−4, +4]. Edge: <2 obs → `NaN`; zero variance → `0.0`.

**Blend** — `_blend_equity_z_score(close_series)` (`src/sector_audit.py:2179-2200`), called at
`:2323`:
```
n        = len(numeric(close).dropna())
win_fast = min(20, max(2, n))
z_fast   = calculate_z_score(s, win_fast)
if n < 45:  z = z_fast,  z_slow = None,  note = "20d_only"
else:
  slow_win = min(60, max(21, n-1))
  z_slow   = calculate_z_score(s, max(2, slow_win))
  z        = round(0.55*z_fast + 0.45*z_slow, 4)
```
- `z_score` = blended value (the field the engine consumes); `z_score_fast_20` = `z_fast`;
  `z_score_slow` = `z_slow` (or `NaN` when `None`, audit field at `:2400`).
- Weights `0.55 / 0.45` and the `45`-session gate are **fixed constants** (not env-tunable).

## 1.2 cmf_20 (Chaikin Money Flow) + cmf_20_delta

**cmf_20** — `calculate_cmf(data, window=20)` (`src/titan_engine.py:176-200`), called at
`:2341`:
```
requires columns {high, low, close, volume}; else NaN
hl_range = (high - low)              # zeros -> NaN
mfm      = ((close - low) - (high - close)) / hl_range     # Money Flow Multiplier, [-1, +1]
mfv      = mfm * volume                                    # Money Flow Volume
roll     = min(window, len(data))
cmf      = rolling_sum(mfv, roll) / rolling_sum(volume, roll)   # vol sum zeros -> NaN
return last non-NaN cmf            # NaN if none
```
- Window 20. Range: [−1, +1]. inf/−inf → `NaN`. Empty/missing cols → `NaN`.

**cmf_20_delta** — `_cmf_delta_payload(prev, cur)` (`src/sector_audit.py:704-725`); previous
value = `calculate_cmf(df.iloc[:-1], 20)` (`:2342-2343`):
```
abs_change = cur - prev
rel_pct    = (abs_change / |prev|) * 100   if |prev| > 1e-9 else None
interpretation via _cmf_delta_interpretation(abs_change, cur)   (:688-701)
```
`_cmf_delta_interpretation` thresholds (constants): `≥0.03 strong_increase`, `>0.005 increase`,
`≤-0.03 strong_decrease`, `<-0.005 decrease`, else by level `>0.05 stable_accumulation` /
`<-0.05 stable_distribution` / else `stable_neutral`. If either value `NaN` →
`interpretation="unavailable"`. **Note:** `cmf_20_delta` is a descriptive payload; the v2
engine does **not** consume it (it consumes raw `cmf_20`).

## 1.3 adx_14 (+ adx_plus_di_14, adx_minus_di_14)

**adx_14** — `calculate_adx(data, window=14)` (`src/titan_engine.py:60-99`), called `:2337`:
```
requires {high, low, close}; window>=2 else NaN
TR        = max(|H-L|, |H-Cprev|, |L-Cprev|)
+DM       = (H - Hprev) if (H-Hprev) > (Lprev-L) else 0, clipped >=0
-DM       = (Lprev - L) if (Lprev-L) > (H-Hprev) else 0, clipped >=0
roll      = min(window, len(data))
+DI       = 100 * rolling_sum(+DM, roll) / rolling_sum(TR, roll)
-DI       = 100 * rolling_sum(-DM, roll) / rolling_sum(TR, roll)
DX        = 100 * |+DI - -DI| / (+DI + -DI)
ADX       = rolling_mean(DX, min(window, len(DX)))
return last ADX        # NaN if empty
```
- Window 14. Range: [0, 100]. Uses **Wilder-style smoothing approximated by simple rolling
  sums/means** (not the classic Wilder EMA). Zero denominators → `NaN` and are dropped.

**adx_plus_di_14 / adx_minus_di_14** — `calculate_latest_di(data, window=14)`
(`src/titan_engine.py:102-136`), called `:2338`. Same `+DI / -DI` as above; returns the latest
non-NaN `(+DI, -DI)`. Range [0, 100]. Missing cols/empty → `(NaN, NaN)`.

## 1.4 obv_slope_20 (On-Balance Volume slope)

`calculate_obv_slope(data, window=20)` (`src/titan_engine.py:203-230`), called `:2344`:
```
requires {close, volume}; window>=2 else NaN
delta     = close.diff()
direction = +1 if delta>0 else (-1 if delta<0 else 0)
OBV       = cumsum(direction * volume.fillna(0))
tail      = OBV[-min(window, len(OBV)):]
slope     = polyfit(x=0..len(tail)-1, y=tail, deg=1)[0]     # least-squares slope
```
- Window 20. Units: OBV (shares) per session — magnitude is instrument-dependent; **only its
  sign** matters to the engine. <2 points or all-NaN → `NaN`.

## 1.5 EMA & ema_200_distance_pct

**EMA** — `calculate_ema(data, span=200)` (`src/titan_engine.py:28-35`), called `:2330`:
```
ema = close.ewm(span=span, adjust=False).mean();  return last
```
- Pandas EWM with `adjust=False`, smoothing `alpha = 2/(span+1)`. Empty → `NaN`.

**ema_200_distance_pct** (`src/sector_audit.py:2331-2335`):
```
ema_200_distance_pct = (close_last / ema_200 - 1) * 100   if ema_200 not in {NaN, 0} else NaN
```
- Units: percent. Positive = price above its 200-EMA. NaN-safe (guards 0 / NaN EMA).

## 1.6 atr_14, atr_14_pct, atr_14_over_atr_63

**atr_14** — `calculate_atr(data, window=14)` (`src/titan_engine.py:38-57`), called `:2336`:
```
requires {high, low, close}; window>=1 else NaN
TR  = max(|H-L|, |H-Cprev|, |L-Cprev|)
ATR = rolling_mean(TR, min(window, len(TR)), min_periods=1);  return last
```
- Simple moving average of True Range (not Wilder EMA). Units: price.

**atr_14_pct** (`src/sector_audit.py:2345-2349`):
```
atr_14_pct = (atr_14 / close_last) * 100   if atr_14, close_last valid and close_last != 0 else NaN
```
- Units: percent of price. Typical equity range ≈ 1–6%.

**atr_14_over_atr_63** — `calculate_atr_ratio(data, 14, 63)` (`src/titan_engine.py:167-173`),
called `:2340`:
```
ratio = atr(14) / atr(63)   if both valid and atr(63) != 0 else NaN
```
- Dimensionless. >1 = short-term volatility expansion vs ~quarter baseline.

## 1.7 ema200_stretch_atr (NEW; signed)

(`src/sector_audit.py:2371-2379`):
```
ema200_stretch_atr = ema_200_distance_pct / atr_14_pct
                     if both valid and atr_14_pct != 0 else NaN
```
- **Signed**, volatility-normalized distance from EMA200, in **ATR-% units** ("how many ATRs of
  stretch"). Positive = extended above EMA200. The C-8 over-extension term reads this field.

## 1.8 sector_pctile_ema200_stretch (NEW; percentile mechanism)

Assigned via `assign_percentile("ema200_stretch_atr", "sector_pctile_ema200_stretch")`
(`src/sector_audit.py:2098`), inside `_apply_sector_cross_section(..., score_percentiles=False)`.

`assign_percentile(src, dst)` (`:2084-2088`): collects all non-NaN values of `src` across the
sector cohort, then for each audit sets `dst = percentile_rank_0_100(vals, this_value)` (or
`NaN`). Requires ≥2 audits in the cohort (`:2057`); single-name cohorts skip this percentile.

`percentile_rank_0_100(values, x)` (`src/tape_metrics.py:19-33`):
```
if isnan(x) or no values: NaN
xs    = sorted(non-NaN values)
below = count(v < x);  equal = count(v == x)
rank  = below + (equal-1)/2   (mid-rank for ties)
pct   = 100 * rank / max(1, len(xs)-1)   if len(xs)>1 else 50.0
```
- Range [0, 100]. Used as a ×1.25 corroborating amplifier when ≥90 (top decile).

## 1.9 gap_down_proxy (NEW; boolean)

(`src/sector_audit.py:2383-2388`):
```
gap_down_proxy = False
if 'open' column present and close_prev valid and != 0:
    open_last = last open
    gap_down_proxy = ((open_last / close_prev) - 1) * 100 <= -1.5
```
- **Tunable threshold (hard-coded constant):** −1.5% open-vs-prior-close. No `open` column → stays
  `False` (no fabricated data).

## 1.10 return_1d/5d/10d/20d_pct

**return_1d_pct** (`src/sector_audit.py:2305-2309`):
```
ret1d = (close_last / close_prev - 1) * 100   if close_prev valid and != 0 else NaN
```
**return_5d/10d/20d_pct** — `pct_return_n_sessions_back(closes, n)` for n=5/10/20
(`src/sector_audit.py:2310-2312`; helper `src/tape_metrics.py:36-45`):
```
s = numeric(closes).dropna()
if len(s) < n+1 or n<1: NaN
return round((s[-1] / s[-(n+1)] - 1) * 100, 4)
```
- Units: percent over n sessions. Needs `n+1` observations.

## 1.11 rel_return_5d/10d/20d_vs_nifty_pct

`benchmark_relative_returns(stock_df, bench_df, close_col, horizons=(5,10,20))`
(`src/tape_metrics.py:68-112`), called `:2315`:
```
inner-join stock & benchmark closes on normalized date
for each horizon n (needs >= max(horizons)+2 joined rows, and >= n+1):
  rs = (stock[-1]/stock[-(n+1)] - 1) * 100
  rb = (bench[-1]/bench[-(n+1)] - 1) * 100
  rel_return_{n}d_vs_nifty_pct = round(rs - rb, 4)
```
- Units: percentage-point excess return vs benchmark. Missing benchmark → all `NaN`.
- Benchmark frame is thread-local (`_THREAD_LOCAL.sector_benchmark_ohlc`, `:2314`).

## 1.12 volume_participation_ratio (5-session basis)

`volume_participation_ratio(ohlc_df)` (`src/breeze_client.py:303-321`), called `:2324`:
```
v = numeric(volume).dropna();  if len(v) < 2: NaN
current = v[-1]
prior   = v[:-1];  tail = prior.tail(5)
avg     = mean(tail) if tail nonempty else mean(prior)
if avg == 0:  return +inf if current>0 else 0.0
return current / avg
```
- **5-session basis:** denominator is the mean of the **prior 5** sessions (excluding today).
- Dimensionless. >1 = above-average turnover; ≥1.5 / ≤0.5 drive stress proxies (§1.16).
- Stored as both `volume_participation_ratio` and legacy alias `absorption_ratio` (`:2402,2406`).
- Calibrated derivatives (`_calibrate_volume_participation_v2`, `:1180-1205`): cap raw at a
  resolved per-instrument cap (default `PARTICIPATION_CAP_DEFAULT = 2.5`, `:43`), then normalize
  to `volume_participation_for_scoring`; the latter feeds `intent_score`. The **raw** ratio is
  what Layer D / Layer B read.

## 1.13 intent_score / effective_intent_score (cash-market composite)

For sector equities the score is the **equity technical** composite (no real PCR):
`calculate_equity_technical_score(z_score, participation_for_scoring)`
(`src/titan_engine.py:297-322`), assigned at `:2361, 2450`:
```
norm_z(z)            = clamp01(0.5 + 0.5*tanh(z/3))        (NaN -> 0.5)
norm_participation(p)= clamp01(p/3)  (+inf -> 1.0, NaN -> 0.5)
score = round(100 * (0.52*norm_z + 0.48*norm_participation), 2)     # 0..100
```
- `intent_score = effective_intent_score = equity_technical_score = score` at build time
  (`:2449-2451`). Range [0, 100].
- `effective_intent_score` is later **mutated downward** by guardrails (§1.18). The engine reads
  `effective_intent_score`, falling back to `intent_score` (`src/signal_v2.py:186, 568`).
- `calculate_intent_score(pcr, z, absorption)` (`:263-294`, weights 0.35/0.35/0.30) is the
  **index/NIFTY** variant with `norm_pcr = clamp01(0.5 + 0.5*atan(p)/(π/2))`; not used for cash
  equities.

## 1.14 next_day_score / next_week_score (predictive composites)

`_predictive_scores(audit)` (`src/sector_audit.py:1238-1335`); assigned by
`_refresh_symbol_scoring_outputs` (`:1427-1431`). Both clamped to [0, 100] via `_clamp_score`
(`:1223-1224`). Inputs: `effective_intent_score` (→`tech`), `return_1d/5d/10d_pct`,
`rel_return_5d/20d_vs_nifty_pct`, `ema_200_distance_pct`, `atr_penalty_input` (fallback
`atr_14_pct`), and EMA history confidence.

Common sub-terms (`:1259-1270`):
```
tech_day  = (tech-50)*0.52          tech_week = (tech-50)*0.62          (0 if tech NaN)
ret1_w    = 0.18 if extreme_price_move_proxy else 0.42
ret_term  = ret1d * ret1_w
ret5_term = ret5d * 0.28            ret10_term = ret10d * 0.15
rel5_term = rel5 * 0.20             rel20_term = rel20 * 0.11
ema_base  = ema_200_distance_pct * 0.26 * ema_conf
ema_day   = ema_base * 0.85         ema_week = ema_base * 1.0
atr_penalty = atr_in * 0.45         (any NaN input contributes 0)
```
`ema_conf = _ema_history_confidence(rows)` (`:1227-1235`): `1.0` if rows non-int; `0.35` if
rows<30; else `min(1.0, max(0.35, rows/200))`.

Composites (`:1272-1293`):
```
next_day  = 50 + tech_day  + ret_term        + ret5_term        + 0.55*ret10_term
               + rel5_term + 0.55*rel20_term + ema_day  - atr_penalty
next_week = 50 + tech_week + 0.78*ret_term   + 0.82*ret5_term   + 0.48*ret10_term
               + 0.72*rel5_term + 0.5*rel20_term + ema_week - 0.35*atr_penalty
```
Event/stress penalties applied after (`:1295-1307`):
- `trap_exit_proxy`: day −8, week −5.
- high-volume-down-day stress (`high_volume_down_day_proxy or panic_absorption_proxy`,
  `:337-339`): day −6, week −4.
- `event_risk_soon`: day −4, week −6.

Range [0, 100]; baseline 50. All coefficients are **fixed constants**.

## 1.15 median_notional_inr_20d & history_lt_200_sessions

**median_notional_inr_20d** — `median_notional_inr_20d(df, close_col)`
(`src/tape_metrics.py:48-58`), called `:2313`:
```
tail   = df.tail(20)
notion = (close * volume).dropna()
return median(notion)   # NaN if empty/missing cols
```
- Units: INR-ish (assumes volume in shares). Liquidity floor compare uses this.

**history_lt_200_sessions** (`src/sector_audit.py:2443`):
```
history_lt_200_sessions = len(close.dropna()) < 200      # bool
```

## 1.16 Boolean proxies & guardrail flags (trigger conditions)

All thresholds below are **hard-coded constants** unless tagged env-tunable.

- **high_volume_down_day_proxy** (`:2362-2364`):
  `ret1d < 0 AND vpr_raw >= 1.5`.
- **panic_absorption_proxy** (`:2439`): **identical alias** of `high_volume_down_day_proxy`.
- **trap_exit_proxy** (`:2365-2367`): `ret1d > 0 AND vpr_raw <= 0.5` (up day on collapsing
  participation → suspect/"hollow" advance).
- **structural_break_proxy** (`:2435-2437`): `atr_break_multiple >= 1.5`, where
  `atr_break_multiple = |close_last - ema_200| / atr_14` (`:2350-2359`, NaN if atr_14<=0).
  Units: ATRs of displacement from EMA200.
- **extreme_price_move_proxy** (`:2316-2322`): `True` if `|ret1d|>=18` OR `|ret5d|>=38` OR
  `|ret10d|>=48` (percent). Down-weights momentum/return terms elsewhere.
- **gap_down_proxy**: see §1.9 (open ≤ −1.5% vs prior close).
- **liquidity_thin_proxy** (`:2113-2123`, cohort path; single-name path `:2061-2068`):
  `thin_hard OR thin_peer` where
  `thin_hard = (0 < median_notional_inr_20d < liquidity_floor)` and
  `thin_peer = (sector_pctile_median_notional_20d <= 15.0)` (bottom-quintile turnover).
  `liquidity_floor = TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR` (**env-tunable**, default
  `₹1,200,000`; `_liquidity_floor_inr` at `:2035-2042` and mirrored in
  `src/signal_v2.py:443-450`). Single-name cohorts (<2 audits) use `thin_hard` only.
- **Cluster guardrail** `_apply_cluster_guardrails` (`:1608-1629`): if >70% of the cohort have
  `ret1d <= -1%`, every name with `effective_intent_score >= 55` is capped to `min(intent, 50)`
  and flagged `cluster_guardrail_applied`.
- **Macro guardrail** `_apply_macro_guardrails` (`:1632-1652`): trigger
  `gift_nifty_change_pct < -0.5 OR india_vix > 18.0`; on trigger,
  `effective_intent_score *= 0.5`, flag `macro_guardrail_applied`.
- **Event guardrail / event_risk_soon** (`_event_flags_for_symbol`, `:2481-2526`):
  `event_risk_soon = (days_to_next_event <= 3)`. `_apply_event_guardrails` (`:1655-1666`)
  multiplies `effective_intent_score *= 0.85` and sets `event_guardrail_applied`.

## 1.17 atr_penalty_input & sector_median_atr_14_pct (Layer-C volatility input)

(`src/sector_audit.py:2100-2111`): sector median of `atr_14_pct` is computed; then per name
`atr_penalty_input = round(min(5.0, atr_14_pct / sector_median_atr_14_pct), 4)` (falls back to
raw `atr_14_pct` if no median, else `NaN`). This is the **sector-relative** volatility input the
C volatility family prefers (§2.C). Range [0, 5].

## 1.18 Mutation ordering note

`effective_intent_score` and the percentile/`atr_penalty_input`/`liquidity_thin_proxy` fields are
written by `_apply_sector_cross_section` and the guardrails **before** the signal engine runs, so
the engine always reads post-guardrail values. `next_week/next_day_score` are recomputed by
`_refresh_symbol_scoring_outputs` (`:1427-1431`).

---

# PART 2 — How metrics drive the waterfall (`src/signal_v2.py`)

Production path: v2 is always on; layers A–E always execute; `accumulate` is a first-class label.
Orchestrator `evaluate_signal_v2` runs **A, C, D, B** in that call order, then aggregates and maps
via E (`:680-750`). Returns `(label, round(risk_net,2), reasons[:8])` and writes `signal_confidence`,
`signal_reason_trace`, `signal_engine_version="v2"` onto the audit.

`CORE_METRICS` (NaN census set, `:77-86`): `z_score, cmf_20, adx_14, ema_200_distance_pct,
atr_14_pct, return_1d_pct, return_5d_pct, return_10d_pct`.
`_SEVERITY` ordering (`:88-94`): `buy 0 < accumulate 1 < hold 2 < trim 3 < exit-risk 4`.

## Layer A — data-quality / sanity (`layer_a`, :130-172)

Outputs `buy_allowed`, `label_ceiling`, `confidence_seed`, `reasons`. **Can only withhold buy /
downgrade confidence / cap label; never asserts buy.**

Config (defaults in code):
- `TITAN_SIGV2_A_NAN_MAX` = **3** (`:144`).
- `TITAN_SIGV2_A_SHORT_HISTORY_CONF` = **0.6** (`:145`).

Logic:
```
nan_count = #{m in CORE_METRICS : isnan(audit[m])}
if nan_count > 0:   seed *= max(0, 1 - 0.05*nan_count)   # each NaN shaves 5%
if nan_count >= 3:  buy_allowed=False; seed *= 0.5
if history_lt_200_sessions: buy_allowed=False; label_ceiling="accumulate"; seed *= 0.6
if liquidity_thin_proxy:    buy_allowed=False
confidence_seed = clamp(seed, 0, 1)
```
`label_ceiling` caps constructive labels only (never blocks downgrades). After Layer E /
hysteresis, `_resolve_layer_a_final_label` re-enforces Layer A: `buy_allowed=False` downgrades
mapped `buy` → `accumulate` or `hold`; `label_ceiling` caps via max `_SEVERITY`.

## Layer C — graded evidence (`layer_c`, :251-353; families `_family_points`, :180-248)

Replaces legacy discrete "step" families with **linear ramps** (via `_ramp`). Layer-D
multipliers are **not** applied here — raw per-term values stay inspectable; aggregation applies
them (§E). A term is traced only if its points `> 0.05` (`:198`).

**Risk families** (`_family_points`, all bearish, each capped):

| Family | Metric | `_ramp(zero_at → full_at, full_points)` | Cap | Line |
|---|---|---|---|---|
| horizon | `next_week_score` | 55 → 45, 3.0 | 3.0 | :211-213 |
| intent | `effective_intent_score` | 52 → 45, 2.0 | 2.0 | :216-218 |
| z | `z_score` (downside only) | −1 → −2, 2.0 | 2.0 | :221-223 |
| momentum | `return_5d/21d/63d/126d_pct` composite | −2→−6 / −4→−12 / −8→−20 / −12→−30 (weights 10/25/35/30) | 3.0 | :431-439 |
| trend | `ema_200_distance_pct` (below only) | −2 → −6, 2.0 | 2.0 | :234 |
| volatility | `atr_penalty_input` (preferred) | 1.25 → 2.2, 2.0 | 2.0 | :240 |
| volatility | else `atr_14_pct` | 4.0 → 6.0, 2.0 | 2.0 | :243 |

- `ret1d_weight = 0.45 if extreme_price_move_proxy else 1.0` (`:195`) — dampens single-day
  shocks. Momentum sub-terms are summed then `min(3.0, ...)` (`:230`).

**C-7 money flow** (`:284-310`) — dead-band ±0.05, scaled by magnitude:
- Config: `TITAN_SIGV2_C_CMF_K` = **10.0** (`:278`); `TITAN_SIGV2_C_CMF_CAP` = **2.0** (`:279`).
```
if cmf < -0.05:                       # distribution -> bear
    money_flow_bear = clamp((-0.05 - cmf) * K, 0, cap)
    if money_flow_bear>0 and obv<0:  money_flow_bear *= 1.25   # OBV amplifies same-sign
elif cmf > 0.05:                      # accumulation -> bull
    money_flow_bull = clamp((cmf - 0.05) * K, 0, cap)
    if money_flow_bull>0 and obv>0:  money_flow_bull *= 1.25
# |cmf| <= 0.05 -> dead-band, both 0
```
The ×1.25 OBV rule only **amplifies an already-nonzero same-sign** term (it never creates one).
`bull_terms = money_flow_bull` (`:351`).

**C-8 ATR-normalized over-extension** (`:312-324`) — upside only:
- Config: `TITAN_SIGV2_C_STRETCH_DEADBAND_ATR` = **4.0** (`:280`);
  `TITAN_SIGV2_C_STRETCH_RAMP_ATR` = **8.0** (`:281`); `TITAN_SIGV2_C_STRETCH_CAP` = **2.0** (`:282`).
```
over_ext = _ramp(ema200_stretch_atr, zero_at=4.0, full_at=8.0, full_points=2.0)
over_extension_hot = (stretch is not NaN) and stretch >= 4.0          # deadband edge
if over_ext>0 and sector_pctile_ema200_stretch >= 90: over_ext *= 1.25  # top-decile corroborator
```
`over_extension_hot` is a key flag consumed by Layers B, D, and the BUY gate.

**Fundamentals** (`:326-342`, graded bear/bull adjustment from `fundamental_status`):
`weak → +2.0`, `balanced → +1.0`, `strong → −1.0`, else `0` (can reduce risk).

When Layer C is disabled, all C outputs are zeroed (`:258-268`).

## Layer D — context modifiers (`layer_d`, :361-435)

Produces multipliers/bumps/flags; **never returns a label** (applied at aggregation). Config
defaults: `TITAN_SIGV2_D_ADX_WEAK`=**20.0** (`:385`), `TITAN_SIGV2_D_ADX_STRONG`=**25.0** (`:386`),
`TITAN_SIGV2_D_DIVERGENCE_RET1D`=**2.0** (`:387`), `TITAN_SIGV2_D_PULLBACK_VPR`=**1.0** (`:388`),
`TITAN_SIGV2_D_STALEFLOW_OBV_EPS`=**0.0** (`:389`). `vpr` reads
`volume_participation_ratio` (fallback `absorption_ratio`, `:383`).

1. **ADX regime weights** (`:621-665`):
   - `adx < 20`: `mult_money_flow=1.3, mult_over_extension=1.3, mult_momentum=0.7` (weak trend →
     up-weight mean-reversion, down-weight momentum).
   - `adx >= 25`: directional (+DI vs −DI) momentum/risk multipliers.
   - `20 <= adx < 25` (deadband) **or ADX NaN**: persist `prev_adx_regime_mults` from prior session
     (never reset to 1.0 when regime is unchanged/unavailable).

2. **Money-flow divergence ("hollow breakout")** (`:406-409`):
   `ret1d > 2.0 AND cmf < -0.05` → `divergence_bump = +1.0` (added to risk) and
   `buy_confidence_cap = 0.5` (caps confidence for any buy/accumulate).

3. **Healthy-pullback rescue** (`:412-421`): all of
   `ret1d < 0 AND vpr < 1.0 AND cmf > 0.05 AND ret5d >= -3.0 AND ema_200_distance_pct >= 0`
   → `mult_momentum = min(mult_momentum, 0.5)` (halve momentum penalty) and
   `pullback_bull_bump = +0.5` (added to bull_c).

4. **Stale-flow OBV tiebreaker (GREAVESCOT rule)** (`:425-432`): all of
   `-0.05 <= cmf <= 0.05` (neutral) `AND over_extension_hot AND adx < 20 AND obv <= eps(0.0)`
   → `staleflow_downgrade = True`. This both **forces TRIM** in Layer B and **counts as a Tier-2
   corroborator** (§B).

## Layer B — two-tier hard disqualifiers (`layer_b`, :453-527)

Runs after C and D (needs `over_extension_hot` and `staleflow_downgrade`). Config:
`TITAN_SIGV2_B_TIER1_GAP_PCT`=**−8.0** (`:465`), `TITAN_SIGV2_B_TIER2_TRIM_COUNT`=**2** (`:466`),
`TITAN_SIGV2_B_TIER2_EXIT_COUNT`=**3** (`:467`).

**Tier-1 instant-exit whitelist** (single signal; `forced_label="exit-risk"`,
`bypass_hysteresis=True`; short-circuits the rest, `:472-489`):
- `t1_structural = structural_break_proxy AND (ret1d <= -8.0 OR gap_down_proxy)`.
- `t1_liquidity = liquidity_thin_proxy AND (0 < median_notional_inr_20d < liquidity_floor)`
  (floor = `TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR`, default ₹1.2M).

**Tier-2 corroboration counting** (`:491-525`) — collect distinct bearish signals:
- `vpr-proxy stress` if any of `trap_exit_proxy / high_volume_down_day_proxy /
  panic_absorption_proxy` — **counts once** (de-dup, because all three derive from the same VPR).
- `cmf distribution` if `cmf < -0.05`.
- `over-extension hot` if `over_extension_hot`.
- `weak ADX with -DI dominance` if `adx < 20 AND minus_di > plus_di`.
- `event risk` if `event_risk_soon OR event_guardrail_applied`.
- `stale-flow downgrade` if Layer-D `staleflow_downgrade`.

```
count = #signals
if count >= 3:                              forced_label = "exit-risk"
elif count >= 2 or staleflow_downgrade:     forced_label = "trim"
```
`corroborators = count` (feeds the confidence formula). Disabled layer → no force (`:461-462`).

## Layer E — aggregation, mapping, confidence, hysteresis (`:535-750`)

**Aggregate** `_aggregate(c, d)` (`:535-558`) — apply D multipliers/bumps to C terms:
```
risk_c  = horizon + intent + z
        + min(3.0, momentum * mult_momentum)        # momentum re-capped at 3
        + trend + volatility
        + money_flow_bear * mult_money_flow
        + over_extension  * mult_over_extension
        + fundamental                                # may be negative
        + divergence_bump
risk_c  = clamp(risk_c, 0, 10)

bull_c  = money_flow_bull * mult_money_flow + pullback_bull_bump
bull_c  = clamp(bull_c, 0, 10)
```

**Net risk** (`:694-697`): `TITAN_SIGV2_E_BULL_OFFSET` = **0.5** (`:694`):
```
risk_net = clamp(risk_c - 0.5 * bull_c, 0, 10)
```
Bull evidence offsets bear at **half weight** — bullish flow can pull a name down the risk
ladder but never directly forces a buy (the BUY gate is independent).

**BUY gate** `_buy_gate(audit, a, c)` (`:561-589`):
```
core = buy_allowed AND not trap_exit_proxy AND not liquidity_thin_proxy
       AND not extreme_price_move_proxy
       AND next_week_score >= 70 AND effective_intent_score >= 65
       AND not (return_5d_pct <= -4) AND not (rel_return_5d_vs_nifty_pct <= -3)
flow_ok          = isnan(cmf) OR cmf >= -0.05
not_overextended = not over_extension_hot
clean_buy        = core AND flow_ok AND not_overextended
constructive_core = core
```
(All thresholds — 70, 65, −4, −3 — are constants in `_buy_gate`.)

**Mapping ladder** `_map_label(risk_net, gate, a)` (`:592-604`):
```
risk_net >= 7:                         exit-risk
risk_net >= 4:                         trim
risk_net < 4 and clean_buy:            buy
risk_net < 4 and constructive_core:    accumulate
otherwise:                             hold
```
Then in order (`:701-707`):
1. `_apply_ceiling` (`:607-611`): if `label_ceiling=="hold"` and label ∈ {buy, accumulate} → `hold`;
   `label_ceiling=="accumulate"` caps buy → accumulate (e.g. short history).
2. `_escalate` (`:614-617`): `forced_label` from Layer B overrides **only if more severe**
   (`_SEVERITY`).

**Note:** `accumulate` is a first-class always-on label; there is no `TITAN_SIGV2_ENABLE_ACCUMULATE`
gate in production.

**Hysteresis** `_apply_hysteresis` (`:620-651`; `TITAN_SIGV2_E_HYST_BUFFER` = **0.5**, `:695`),
applied only if Layer E enabled (`:711-714`):
```
prior = audit["prev_action_signal"]  (lowercased; None if absent)
if prior is None or prior == label:        no-op
if bypass or label == "exit-risk":         no-op (danger fast-path)
if {prior,label}=={hold,trim}:
    trim & risk_net < 4+buffer -> stay prior
    hold & risk_net > 4-buffer -> stay prior
if label in {buy,accumulate} and risk_net > 4-buffer -> stay prior   # constructive entry needs margin
```
- **Asymmetric:** danger transitions (and Tier-1 bypass) apply same-day; constructive transitions
  are sticky around the trim edge (4.0 ± 0.5).
- **Deferred persistence:** the intended 2-session persistence is **not yet active** — `prev_action_signal`
  is only ever populated by the backtest harness (`src/signal_v2_backtest.py:268`) and never in
  production, so live runs hit the `prior is None → no-op` branch (module note, `:14-18, 632-634`).

**Confidence** `_confidence(...)` (`:654-677`):
```
if bypass:                       return clamp(max(0.9, seed), 0, 1)
dominant_bear = label in {trim, exit-risk} or risk_net >= 4
if dominant_bear: n = #bear trace terms (points>0.05) + corroborators
else:             n = #bull trace terms (points>0.05)
base = clamp(0.4 + 0.1*n, 0, 0.95)
conf = base * confidence_seed
if label in {buy, accumulate} and buy_confidence_cap is not None:
    conf = min(conf, buy_confidence_cap)     # divergence cap 0.5
return round(clamp(conf,0,1), 3)
```

**Reasons** assembled B → D → C-trace → A (`:728-735`), truncated to 8 (`:750`).

## Precedence summary (what short-circuits what)

1. **Master/ablation flags** decide whether the engine (or a layer) runs at all (`:59-66`).
2. **Tier-1 (Layer B)** is the only true short-circuit inside B: it sets `exit-risk` +
   `bypass_hysteresis` and returns immediately (`:481-489`). It still does **not** early-return
   from the orchestrator — A/C/D already ran — but it dominates the final label via `_escalate`
   and skips hysteresis + forces high confidence.
3. **Score path** (C→D→E) always computes `risk_net` and a base label; there are **no early-return
   inversions**, because D only reshapes weights (never emits a label) and C never applies D's
   multipliers itself. This keeps the raw trace inspectable and guarantees deterministic ordering.
4. **Escalation is monotonic by severity:** Layer-B `forced_label` can only push the label *more
   bearish* (`_escalate` compares `_SEVERITY`), never upgrade it. So bearish disqualifiers always
   win ties against constructive mappings.
5. **Ceiling vs forced:** `label_ceiling="hold"` only caps the *constructive* side (buy/accumulate
   → hold) and is applied **before** escalation, so it can never block a downgrade to trim/exit.
6. **Bull vs bear netting:** bull evidence enters only through `bull_c` and offsets bear at
   `0.5×` in `risk_net` (`:697`); it can lower the ladder rung but cannot itself satisfy the BUY
   gate, which is an independent conjunction of fundamentals/flow/over-extension checks.
7. **Hysteresis is last and asymmetric:** it can hold a prior label across the trim edge but is
   bypassed for genuine danger (`exit-risk` / Tier-1). Currently a no-op in production pending
   `prev_action_signal` plumbing.

### Config key quick-reference (key → default → file:line)

| Key | Default | Line |
|---|---|---|
| `TITAN_SIGV2_A_NAN_MAX` | 3 | :144 |
| `TITAN_SIGV2_A_SHORT_HISTORY_CONF` | 0.6 | :145 |
| `TITAN_SIGV2_C_CMF_K` | 10.0 | :278 |
| `TITAN_SIGV2_C_CMF_CAP` | 2.0 | :279 |
| `TITAN_SIGV2_C_STRETCH_DEADBAND_ATR` | 4.0 | :280 |
| `TITAN_SIGV2_C_STRETCH_RAMP_ATR` | 8.0 | :281 |
| `TITAN_SIGV2_C_STRETCH_CAP` | 2.0 | :282 |
| `TITAN_SIGV2_D_ADX_WEAK` | 20.0 | :385 |
| `TITAN_SIGV2_D_ADX_STRONG` | 25.0 | :386 |
| `TITAN_SIGV2_D_DIVERGENCE_RET1D` | 2.0 | :387 |
| `TITAN_SIGV2_D_PULLBACK_VPR` | 1.0 | :388 |
| `TITAN_SIGV2_D_STALEFLOW_OBV_EPS` | 0.0 | :389 |
| `TITAN_SIGV2_B_TIER1_GAP_PCT` | −8.0 | :465 |
| `TITAN_SIGV2_B_TIER2_TRIM_COUNT` | 2 | :466 |
| `TITAN_SIGV2_B_TIER2_EXIT_COUNT` | 3 | :467 |
| `TITAN_SIGV2_E_BULL_OFFSET` | 0.5 | :694 |
| `TITAN_SIGV2_E_HYST_BUFFER` | 0.5 | :695 |
| `TITAN_MIN_MEDIAN_DAILY_NOTIONAL_INR` | 1,200,000 | :443-450 |

> **Verification note:** every default tabulated above was read directly from
> `src/signal_v2.py`. All values stated in the original task brief match the code exactly —
> no discrepancies were found (K=10.0, cmf cap=2.0, stretch dead-band=4.0/ramp=8.0/cap=2.0,
> ADX 20/25, divergence ret1d>2.0, gap tier-1 −8.0, trim≥2 / exit≥3, bull offset 0.5, hysteresis
> buffer 0.5, accumulate default off). The only "gotcha" worth flagging to a validator is that
> the 2-session hysteresis persistence is **defined but inert in production** because
> `prev_action_signal` is never populated outside the backtest harness.
