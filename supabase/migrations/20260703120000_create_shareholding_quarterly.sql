-- Quarterly shareholding / free-float for breakout liquidity scoring (Phase 4).
-- Mirrors sql/create_shareholding_quarterly.sql.

create table if not exists public.shareholding_quarterly (
    symbol                text         not null,
    as_of_date            date         not null,
    free_float_pct        double precision not null,
    promoter_holding_pct  double precision,
    source                text         not null default 'nse_corporate_share_holdings_master',
    ingested_at           timestamptz  not null default now(),
    primary key (symbol, as_of_date)
);
create index if not exists idx_shareholding_quarterly_symbol
    on public.shareholding_quarterly (symbol, as_of_date desc);
