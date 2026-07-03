-- Daily equity OHLCV cache for breakout scanner (Phase 4).
-- Mirrors sql/create_equity_ohlcv_daily.sql.

create table if not exists public.equity_ohlcv_daily (
    symbol       text             not null,
    trade_date   date             not null,
    open         double precision not null,
    high         double precision not null,
    low          double precision not null,
    close        double precision not null,
    volume       bigint           not null,
    source       text             not null default 'yahoo',
    ingested_at  timestamptz      not null default now(),
    primary key (symbol, trade_date)
);
create index if not exists idx_equity_ohlcv_daily_symbol
    on public.equity_ohlcv_daily (symbol, trade_date desc);
