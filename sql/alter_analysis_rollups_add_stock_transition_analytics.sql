-- Idempotent extension for stock-level signal transition analytics persistence.
-- Safe to run multiple times; avoids destructive schema operations.

alter table if exists public.symbol_daily_features
    add column if not exists action_signal text;

create table if not exists public.stock_signal_transition_analytics (
    trade_date date not null,
    sector text not null,
    symbol text not null,
    exchange text not null,
    run_id text null references public.run_metadata(run_id) on delete set null,
    trailing_window_days integer not null default 30,
    previous_signal text null,
    current_signal text not null,
    transition_type text not null,
    transition_date date null,
    days_in_previous_signal integer null,
    buy_signal_consistency_ratio double precision not null default 0,
    hold_signal_consistency_ratio double precision not null default 0,
    trim_signal_consistency_ratio double precision not null default 0,
    exit_risk_signal_consistency_ratio double precision not null default 0,
    transition_stability_score double precision not null default 0,
    is_whipsaw_transition boolean not null default false,
    whipsaw_transition_count integer not null default 0,
    transition_event_count integer not null default 0,
    matured_1w_available boolean not null default false,
    matured_1w_realized_return_pct double precision null,
    matured_1w_outcome text null,
    matured_1m_available boolean not null default false,
    matured_1m_realized_return_pct double precision null,
    matured_1m_outcome text null,
    computed_at timestamptz not null default now(),
    primary key (trade_date, sector, symbol, exchange, trailing_window_days)
);

create index if not exists idx_stock_signal_transition_sector_date
    on public.stock_signal_transition_analytics (sector, trade_date desc);

create index if not exists idx_stock_signal_transition_symbol_date
    on public.stock_signal_transition_analytics (symbol, exchange, trade_date desc);

-- Validation helper checks:
-- 1) Run coverage by day/sector:
-- select trade_date, sector, count(*) as row_count
-- from public.stock_signal_transition_analytics
-- where trade_date = current_date
-- group by trade_date, sector
-- order by trade_date desc, sector;
--
-- 2) Outcome maturity distribution:
-- select sector, matured_1w_available, matured_1m_available, count(*) as symbols
-- from public.stock_signal_transition_analytics
-- where trade_date = current_date
-- group by sector, matured_1w_available, matured_1m_available
-- order by sector, matured_1w_available desc, matured_1m_available desc;
--
-- 3) Whipsaw hotspots:
-- select sector, symbol, exchange, transition_type, whipsaw_transition_count, transition_stability_score
-- from public.stock_signal_transition_analytics
-- where trade_date = current_date and whipsaw_transition_count > 0
-- order by whipsaw_transition_count desc, transition_stability_score asc
-- limit 50;
