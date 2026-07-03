-- Quarterly shareholding / free-float (additive, idempotent).
-- Apply with: python scripts/apply_shareholding_quarterly_migration.py (needs SUPABASE_ACCESS_TOKEN)
-- Natural-key PK so quarterly ingest can upsert idempotently.

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
