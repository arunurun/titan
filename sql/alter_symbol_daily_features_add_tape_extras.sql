-- Optional calibration / tape fields (JSON). Run after create_analysis_rollups.sql.
alter table if exists public.symbol_daily_features
    add column if not exists tape_extras jsonb not null default '{}'::jsonb;

comment on column public.symbol_daily_features.tape_extras is
    'Multi-day returns, Nifty-relative alphas, liquidity proxy, sector percentiles, etc.';
