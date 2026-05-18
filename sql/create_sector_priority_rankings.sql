-- Persistent sector priority rankings (pilot: ai sector).
-- Run in Supabase SQL editor before enabling --sector-priority-only.

create extension if not exists pgcrypto;

create table if not exists public.sector_priority_rankings (
    id uuid primary key default gen_random_uuid(),
    sector_key text not null,
    symbol text not null,
    exchange text not null,
    as_of_date date not null,
    market_cap_inr_cr numeric null,
    market_cap_bucket text not null,
    return_1w_pct numeric null,
    return_1m_pct numeric null,
    absorption_ratio numeric null,
    rank_score numeric not null,
    rank_in_sector integer not null,
    is_priority boolean not null default false,
    meta jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_priority_rankings_sector_lower check (sector_key = lower(sector_key)),
    constraint sector_priority_rankings_symbol_upper check (symbol = upper(symbol)),
    constraint sector_priority_rankings_exchange_check check (exchange in ('NSE', 'BSE')),
    constraint sector_priority_rankings_bucket_check check (
        market_cap_bucket in ('micro', 'small', 'mid', 'large', 'unknown')
    ),
    unique (sector_key, symbol, exchange, as_of_date)
);

create index if not exists idx_sector_priority_rankings_lookup
    on public.sector_priority_rankings (sector_key, as_of_date, is_priority, rank_in_sector);

