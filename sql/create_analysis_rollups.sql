-- Titan analysis foundation: run metadata + daily features + rollups.
-- Run in Supabase SQL Editor before enabling TITAN_ENABLE_ANALYSIS_STORE=1.

create extension if not exists pgcrypto;

create table if not exists public.run_metadata (
    run_id text primary key,
    run_ts timestamptz not null,
    trade_date date not null,
    sector text not null,
    mode text not null,
    status text not null,
    symbol_count integer not null default 0,
    ok_count integer not null default 0,
    meta jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.symbol_daily_features (
    trade_date date not null,
    sector text not null,
    symbol text not null,
    exchange text not null,
    run_id text null references public.run_metadata(run_id) on delete set null,
    run_ts timestamptz not null,
    intent_score double precision null,
    effective_intent_score double precision null,
    z_score double precision null,
    absorption_ratio double precision null,
    return_1d_pct double precision null,
    ema_200_distance_pct double precision null,
    atr_14_pct double precision null,
    flags jsonb not null default '[]'::jsonb,
    option_chain_unavailable boolean not null default false,
    rows_count integer not null default 0,
    primary key (trade_date, sector, symbol, exchange)
);

create table if not exists public.sector_daily_rollup (
    trade_date date not null,
    sector text not null,
    run_id text null references public.run_metadata(run_id) on delete set null,
    run_ts timestamptz not null,
    symbol_count integer not null default 0,
    avg_intent_score double precision null,
    median_intent_score double precision null,
    avg_effective_intent_score double precision null,
    breadth_above_ema200_pct double precision null,
    pct_z_gt_2 double precision null,
    pct_absorption_gt_1 double precision null,
    trap_count integer not null default 0,
    panic_absorption_count integer not null default 0,
    macro_guardrail_count integer not null default 0,
    cluster_guardrail_count integer not null default 0,
    event_guardrail_count integer not null default 0,
    primary key (trade_date, sector)
);

create table if not exists public.sector_period_rollup (
    period_type text not null,
    period_end date not null,
    sector text not null,
    window_days integer not null,
    avg_intent_score double precision null,
    avg_effective_intent_score double precision null,
    breadth_above_ema200_pct double precision null,
    pct_z_gt_2 double precision null,
    pct_absorption_gt_1 double precision null,
    trap_count integer not null default 0,
    panic_absorption_count integer not null default 0,
    source_trade_days integer not null default 0,
    updated_at timestamptz not null default now(),
    primary key (period_type, period_end, sector)
);

create table if not exists public.llm_digest_memory (
    run_id text primary key,
    sector text not null,
    prompt_facts jsonb not null default '{}'::jsonb,
    output_text text not null,
    full_digest text,
    model_name text not null default '',
    output_chars integer not null default 0,
    recorded_at timestamptz not null default now()
);

create index if not exists idx_run_metadata_sector_date
    on public.run_metadata (sector, trade_date desc);

create index if not exists idx_symbol_daily_features_symbol_date
    on public.symbol_daily_features (symbol, exchange, trade_date desc);

create index if not exists idx_sector_daily_rollup_sector_date
    on public.sector_daily_rollup (sector, trade_date desc);

create index if not exists idx_sector_period_rollup_sector_period
    on public.sector_period_rollup (sector, period_type, period_end desc);

create index if not exists idx_llm_digest_memory_sector_recorded
    on public.llm_digest_memory (sector, recorded_at desc);
