-- Run manually in Supabase SQL Editor (Dashboard → SQL → New query).
-- Adds fundamental_score for analysis_store symbol_daily_features upserts.
-- Until applied, Titan omits the column on upsert and keeps scores in tape_extras.

alter table if exists public.symbol_daily_features
    add column if not exists fundamental_score double precision null;

comment on column public.symbol_daily_features.fundamental_score is
    'Fundamental quality score (0–100) from fundamental_engine; mirrored in tape_extras.';
