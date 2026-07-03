-- Run manually in Supabase SQL Editor (Dashboard → SQL → New query).
-- Adds fundamental quality columns to market_instruments for fundamental_engine scoring.
-- Populate via: python scripts/ingest_fundamentals.py --all

alter table if exists public.market_instruments
    add column if not exists roe double precision null,
    add column if not exists roce double precision null,
    add column if not exists debt_to_equity double precision null,
    add column if not exists net_profit_margin double precision null,
    add column if not exists operating_margin double precision null,
    add column if not exists revenue_growth_pct double precision null,
    add column if not exists eps_growth_pct double precision null,
    add column if not exists pe_ratio double precision null,
    add column if not exists peg_ratio double precision null,
    add column if not exists price_to_sales double precision null,
    add column if not exists free_cash_flow double precision null,
    add column if not exists market_cap double precision null,
    add column if not exists fcf_yield_pct double precision null,
    add column if not exists fundamentals_as_of date null,
    add column if not exists fundamentals_source text null;

comment on column public.market_instruments.roe is
    'Return on equity (%); used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.roce is
    'Return on capital employed (%); used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.debt_to_equity is
    'Debt-to-equity ratio; used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.net_profit_margin is
    'Net profit margin (%); used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.operating_margin is
    'Operating margin (%); fallback margin input for fundamental_engine.';
comment on column public.market_instruments.revenue_growth_pct is
    'YoY revenue growth (%); used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.eps_growth_pct is
    'YoY EPS / earnings growth (%); used for PEG in fundamental_engine.';
comment on column public.market_instruments.pe_ratio is
    'Trailing P/E ratio; used by fundamental_engine.score_fundamentals.';
comment on column public.market_instruments.peg_ratio is
    'PEG ratio when available from source; otherwise derived in engine.';
comment on column public.market_instruments.price_to_sales is
    'Price-to-sales (TTM); informational.';
comment on column public.market_instruments.free_cash_flow is
    'Free cash flow (INR or source currency); used for FCF yield.';
comment on column public.market_instruments.market_cap is
    'Market capitalization (INR or source currency); used for FCF yield.';
comment on column public.market_instruments.fcf_yield_pct is
    'Free cash flow yield (% of market cap); stored or computed at ingest.';
comment on column public.market_instruments.fundamentals_as_of is
    'As-of date for the fundamental snapshot.';
comment on column public.market_instruments.fundamentals_source is
    'Data provider for fundamentals (e.g. yfinance).';

create index if not exists idx_market_instruments_fundamentals_as_of
    on public.market_instruments (fundamentals_as_of desc nulls last)
    where fundamentals_as_of is not null;
