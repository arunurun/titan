-- TITAN V12 news integration schema (Phase 1).
-- Deploy: psql -h <SUPABASE_HOST> -U postgres -d postgres -f sql/create_news_tables.sql
-- Or paste into Supabase SQL Editor.
--
-- Schema conflict resolution:
--   * KEEP macro public.global_news_snapshots (sector_priority / Cloudflare worker insert shape).
--   * ADD per-symbol cache in public.symbol_news_snapshots (news.md aggregate snapshot schema).
--   * ADD public.news_feed + public.news_sentiment_cache (news.md item + sentiment cache).
--   * EXTEND public.symbol_daily_features with news correlation columns.

-- ---------------------------------------------------------------------------
-- Macro snapshot table (sector-wide RSS/news themes; NOT per-symbol news.md DDL)
-- ---------------------------------------------------------------------------
create table if not exists public.global_news_snapshots (
    id bigserial primary key,
    refreshed_at timestamptz not null,
    item_count integer not null default 0,
    fetch_status text not null default 'ok',
    refresh_error text not null default '',
    news_items jsonb not null default '[]'::jsonb,
    sector_scores jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_global_news_snapshots_refreshed_at
    on public.global_news_snapshots (refreshed_at desc);

comment on table public.global_news_snapshots is
    'Macro RSS/news snapshot for sector_priority blending (insert-only rows).';

-- ---------------------------------------------------------------------------
-- Per-symbol news items (dedupe by url)
-- ---------------------------------------------------------------------------
create table if not exists public.news_feed (
    id bigserial primary key,
    symbol varchar(20) not null,
    exchange varchar(10) not null default 'NSE',
    title text not null,
    url text not null,
    source varchar(50) not null,
    published_at timestamptz not null,
    fetched_at timestamptz default now(),
    sentiment varchar(20) not null default 'neutral',
    sentiment_score double precision default 0.0,
    sentiment_model varchar(50) default 'vader',
    relevance_score double precision default 0.5,
    is_duplicate boolean default false,
    duplicate_of_id bigint,
    summary text,
    event_type varchar(50),
    impact_level varchar(20) default 'medium',
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    constraint news_feed_url_unique unique (url),
    constraint news_feed_exchange_check check (exchange in ('NSE', 'BSE')),
    constraint news_feed_sentiment_check check (sentiment in ('positive', 'negative', 'neutral', 'mixed')),
    constraint news_feed_sentiment_model_check check (sentiment_model in ('vader', 'finbert')),
    constraint news_feed_impact_level_check check (impact_level in ('high', 'medium', 'low')),
    constraint news_feed_event_type_check check (
        event_type is null
        or event_type in ('earnings', 'acquisition', 'regulatory', 'dividend', 'general')
    )
);

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'news_feed_duplicate_of_id_fkey'
    ) then
        alter table public.news_feed
            add constraint news_feed_duplicate_of_id_fkey
            foreign key (duplicate_of_id) references public.news_feed (id);
    end if;
end $$;

create index if not exists idx_news_symbol_published
    on public.news_feed (symbol, published_at desc);

create index if not exists idx_news_source
    on public.news_feed (source);

create index if not exists idx_news_fetched
    on public.news_feed (fetched_at desc);

create index if not exists idx_news_url
    on public.news_feed (url);

create index if not exists idx_news_exchange
    on public.news_feed (exchange);

comment on table public.news_feed is
    'Normalized per-symbol news items. source examples: newsapi, finnhub, rss:moneycontrol.';

-- ---------------------------------------------------------------------------
-- Sentiment computation cache (1:1 with news_feed)
-- ---------------------------------------------------------------------------
create table if not exists public.news_sentiment_cache (
    id bigserial primary key,
    news_id bigint not null,
    title_hash varchar(64) not null,
    content_hash varchar(64),
    sentiment varchar(20),
    sentiment_score double precision,
    confidence double precision,
    model_used varchar(50),
    computation_time_ms double precision,
    created_at timestamptz default now(),
    constraint news_sentiment_cache_news_id_unique unique (news_id)
);

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'news_sentiment_cache_news_id_fkey'
    ) then
        alter table public.news_sentiment_cache
            add constraint news_sentiment_cache_news_id_fkey
            foreign key (news_id) references public.news_feed (id) on delete cascade;
    end if;
end $$;

create index if not exists idx_sentiment_cache_hash
    on public.news_sentiment_cache (title_hash);

create index if not exists idx_sentiment_cache_news
    on public.news_sentiment_cache (news_id);

-- ---------------------------------------------------------------------------
-- Per-symbol aggregated news snapshots (news.md cache table)
-- ---------------------------------------------------------------------------
create table if not exists public.symbol_news_snapshots (
    id bigserial primary key,
    snapshot_at timestamptz not null,
    symbol varchar(20) not null,
    news_count integer default 0,
    recent_news_items jsonb,
    aggregate_sentiment varchar(20),
    aggregate_score double precision,
    sentiment_trend double precision,
    top_drivers jsonb,
    event_alerts jsonb,
    ttl_seconds integer default 7200,
    created_at timestamptz default now()
);

create index if not exists idx_symbol_snapshots_symbol_time
    on public.symbol_news_snapshots (symbol, snapshot_at desc);

comment on table public.symbol_news_snapshots is
    'Per-symbol news aggregate cache. recent_news_items[]: title, source, published_at, sentiment, sentiment_score.';

-- ---------------------------------------------------------------------------
-- Extend analysis store daily features
-- ---------------------------------------------------------------------------
alter table if exists public.symbol_daily_features
    add column if not exists news_correlation jsonb;

alter table if exists public.symbol_daily_features
    add column if not exists news_sentiment_aggregate varchar(20);

alter table if exists public.symbol_daily_features
    add column if not exists news_sentiment_score double precision;

alter table if exists public.symbol_daily_features
    add column if not exists news_sentiment_trend varchar(20);

alter table if exists public.symbol_daily_features
    add column if not exists news_count integer;

create index if not exists idx_symbol_features_news_sentiment
    on public.symbol_daily_features (symbol, news_sentiment_aggregate);

comment on column public.symbol_daily_features.news_correlation is
    'JSON: driver, affected_metric, affected_theme, direction, confidence, evidence.*, driver_source, stock_news_fetched_count, stock_news_coverage, available.';

-- ---------------------------------------------------------------------------
-- Grants (service_role used by TITAN; anon/authenticated read where noted)
-- ---------------------------------------------------------------------------
grant usage on schema public to anon, authenticated, service_role;

grant select, insert, update, delete on table public.news_feed to service_role;
grant select on table public.news_feed to anon, authenticated;

grant select, insert, update, delete on table public.news_sentiment_cache to service_role;
grant select on table public.news_sentiment_cache to anon, authenticated;

grant select, insert, update, delete on table public.symbol_news_snapshots to service_role;
grant select on table public.symbol_news_snapshots to anon, authenticated;

grant select, insert, update, delete on table public.global_news_snapshots to service_role;
grant select on table public.global_news_snapshots to anon, authenticated;
