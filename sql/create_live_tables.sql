-- Titan live-stream tables (Phase 1 skeleton — additive, idempotent).
-- Apply with: python scripts/apply_live_tables_migration.py  (needs SUPABASE_ACCESS_TOKEN)
-- Or paste into the Supabase SQL editor. PostgREST cannot run DDL.

-- Gate-facing regime snapshots (one row per index per snapshot batch).
-- regime_state ∈ {risk_on, neutral, risk_off, rolling_over}
create table if not exists public.live_regime_snapshots (
    snapshot_ts         timestamptz      not null default now(),
    index_code          text             not null,
    last                double precision,
    pct_vs_prev_close   double precision,
    pct_vs_open         double precision,
    slope_proxy         double precision,
    regime_state        text             not null
        check (regime_state in ('risk_on', 'neutral', 'risk_off', 'rolling_over')),
    source              text             not null default 'breeze_ws_quote',
    ingested_at         timestamptz      not null default now(),
    primary key (snapshot_ts, index_code)
);

create index if not exists idx_live_regime_snapshots_index_ts
    on public.live_regime_snapshots (index_code, snapshot_ts desc);

-- Phase 2+ placeholders (not created in Phase 1 skeleton):
-- live_ticks, intraday_bars, live_intraday_features — see TITAN_LIVE_DATA_PLAN.md §3.2
