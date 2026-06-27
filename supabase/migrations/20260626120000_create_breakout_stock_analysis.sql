-- Breakout scanner per-ticker analysis (one row per ticker per run).
-- Column names match proto/breakout/v1/breakout_scan.proto BreakoutStockAnalysisRecord.
-- Upsert conflict target: (run_id, ticker) -- see src/breakout_store.py.
-- Apply with: python scripts/apply_breakout_stock_analysis_migration.py (needs SUPABASE_ACCESS_TOKEN)

create table if not exists public.breakout_stock_analysis (
    run_id uuid not null,
    scan_date date not null,
    inserted_at timestamptz not null default now(),

    ticker text not null,
    tier text not null,
    symbol_yahoo text not null,
    fetch_error text,
    bar_count integer,
    latest_close double precision,
    prev_close double precision,
    pct_change double precision,
    latest_volume double precision,
    vol_20_avg double precision,
    vol_mult double precision,
    rsi_14 double precision,
    adx_14 double precision,
    sma_50 double precision,
    sma_200 double precision,
    poc_30d double precision,
    min_price_threshold double precision,
    vol_mult_threshold double precision,
    price_above_sma50 boolean,
    yahoo_as_of_date date,

    passed boolean not null default false,
    fail_reason text,
    entry_low double precision,
    entry_high double precision,
    stop_loss double precision,
    target_price double precision,
    target_gain_pct double precision,

    -- v7 evidence / ranking (nullable for pre-v7 rows)
    signal_tier text,
    persistence_score integer,
    composite_rank double precision,
    liquidity_quality double precision,
    breakout_stage integer,
    base_score double precision,
    pass_paths text,
    risk_flags text,

    primary key (run_id, ticker)
);

create index if not exists idx_breakout_stock_analysis_scan_date
    on public.breakout_stock_analysis (scan_date desc);

create index if not exists idx_breakout_stock_analysis_ticker
    on public.breakout_stock_analysis (ticker);

create index if not exists idx_breakout_stock_analysis_run_id
    on public.breakout_stock_analysis (run_id);

create index if not exists idx_breakout_stock_analysis_passed
    on public.breakout_stock_analysis (passed)
    where passed = true;

comment on table public.breakout_stock_analysis is
    'Flattened breakout scanner Yahoo raw values and pass/fail outcome per ticker per run.';
