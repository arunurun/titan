-- Supabase schema for sector-aware market universe storage.
-- Run this in Supabase SQL editor before enabling Titan Supabase registry reads.

create extension if not exists pgcrypto;

create table if not exists public.sector_catalog (
    id uuid primary key default gen_random_uuid(),
    sector_key text not null unique,
    sector_name text not null,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_catalog_key_lower check (sector_key = lower(sector_key))
);

create table if not exists public.market_instruments (
    id uuid primary key default gen_random_uuid(),
    exchange text not null,
    symbol text not null,
    instrument_name text null,
    isin text null,
    breeze_stock_code text null,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint market_instruments_exchange_check check (exchange in ('NSE', 'BSE')),
    constraint market_instruments_symbol_upper check (symbol = upper(symbol)),
    unique (exchange, symbol)
);

create table if not exists public.instrument_sector_map (
    id uuid primary key default gen_random_uuid(),
    instrument_id uuid not null references public.market_instruments(id) on delete cascade,
    sector_id uuid not null references public.sector_catalog(id) on delete cascade,
    source text not null default 'official',
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint instrument_sector_map_source_check check (source in ('official', 'override')),
    unique (instrument_id, sector_id)
);

create table if not exists public.sector_overrides (
    id uuid primary key default gen_random_uuid(),
    exchange text not null,
    symbol text not null,
    sector_key text not null references public.sector_catalog(sector_key),
    reason text null,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_overrides_exchange_check check (exchange in ('NSE', 'BSE')),
    constraint sector_overrides_symbol_upper check (symbol = upper(symbol)),
    unique (exchange, symbol)
);

create table if not exists public.scanner_runs (
    id uuid primary key default gen_random_uuid(),
    status text not null,
    source_name text not null default 'nse_bse_weekly',
    total_seen integer not null default 0,
    inserted_count integer not null default 0,
    updated_count integer not null default 0,
    deactivated_count integer not null default 0,
    message text null,
    started_at timestamptz not null default now(),
    completed_at timestamptz null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint scanner_runs_status_check check (status in ('running', 'completed', 'failed'))
);

create index if not exists idx_market_instruments_active_exchange_symbol
    on public.market_instruments (is_active, exchange, symbol);

create index if not exists idx_instrument_sector_map_sector_active
    on public.instrument_sector_map (sector_id, is_active);

create index if not exists idx_sector_overrides_active
    on public.sector_overrides (is_active, exchange, symbol);

create index if not exists idx_scanner_runs_started_at
    on public.scanner_runs (started_at desc);
