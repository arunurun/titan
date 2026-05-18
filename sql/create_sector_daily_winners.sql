-- Persistent daily winner picks derived from sector priority rankings.
-- Pilot scope: ai sector, but schema is sector-generic.

create extension if not exists pgcrypto;

create table if not exists public.sector_daily_winners (
    id uuid primary key default gen_random_uuid(),
    sector_key text not null,
    as_of_date date not null,
    winner_rank integer not null,
    symbol text not null,
    exchange text not null,
    rank_score numeric not null,
    market_cap_bucket text not null,
    score_breakdown jsonb not null default '{}'::jsonb,
    issue_flags jsonb not null default '[]'::jsonb,
    source_meta jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_daily_winners_sector_lower check (sector_key = lower(sector_key)),
    constraint sector_daily_winners_symbol_upper check (symbol = upper(symbol)),
    constraint sector_daily_winners_exchange_check check (exchange in ('NSE', 'BSE')),
    constraint sector_daily_winners_bucket_check check (
        market_cap_bucket in ('micro', 'small', 'mid', 'large', 'unknown')
    ),
    unique (sector_key, as_of_date, winner_rank),
    unique (sector_key, as_of_date, symbol, exchange)
);

create index if not exists idx_sector_daily_winners_lookup
    on public.sector_daily_winners (sector_key, as_of_date, winner_rank);

