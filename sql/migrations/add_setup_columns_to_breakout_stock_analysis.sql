-- Optional PRE_BREAKOUT setup columns for breakout_stock_analysis.
-- Mirror in supabase/migrations/*_add_setup_columns_breakout.sql

alter table public.breakout_stock_analysis
    add column if not exists setup_trigger_price double precision,
    add column if not exists setup_rank double precision;

comment on column public.breakout_stock_analysis.setup_trigger_price is
    '30d consolidation high; breakout trigger reference for PRE_BREAKOUT rows.';

comment on column public.breakout_stock_analysis.setup_rank is
    'Setup-specific composite rank (0-100) for PRE_BREAKOUT tier sorting.';
