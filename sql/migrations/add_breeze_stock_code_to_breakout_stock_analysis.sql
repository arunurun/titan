-- Add ICICI Breeze stock_code to breakout_stock_analysis.
-- Mirror in supabase/migrations/*_add_breeze_stock_code_breakout.sql

alter table public.breakout_stock_analysis
    add column if not exists breeze_stock_code text;

comment on column public.breakout_stock_analysis.breeze_stock_code is
    'ICICI Breeze stock_code for APIs/order entry; NSE symbol when no alias exists.';

create index if not exists idx_breakout_stock_analysis_breeze_code
    on public.breakout_stock_analysis (breeze_stock_code)
    where breeze_stock_code is not null;
