-- Idempotent columns required for Titan analytics persist + reconcile.
-- Safe to run multiple times. Apply before/after enabling TITAN_ENABLE_ANALYSIS_STORE.

alter table if exists public.symbol_daily_features
    add column if not exists tape_extras jsonb not null default '{}'::jsonb;

alter table if exists public.symbol_daily_features
    add column if not exists action_signal text;

alter table if exists public.symbol_daily_features
    add column if not exists volume_participation_ratio double precision;

alter table if exists public.symbol_daily_features
    add column if not exists next_day_score double precision;

alter table if exists public.symbol_daily_features
    add column if not exists next_week_score double precision;

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

alter table if exists public.sector_daily_rollup
    add column if not exists pct_volume_participation_gt_1 double precision;

comment on column public.symbol_daily_features.volume_participation_ratio is
    'Volume participation ratio (VPR); legacy name absorption_ratio retained for compatibility.';

comment on column public.symbol_daily_features.next_day_score is
    'Denormalized next-day predictive score for reconcile queries; canonical copy also in tape_extras.';

comment on column public.symbol_daily_features.next_week_score is
    'Denormalized next-week predictive score for reconcile queries; canonical copy also in tape_extras.';
