-- Run manually in Supabase SQL Editor (Dashboard → SQL → New query).
-- Adds accumulate_signal_consistency_ratio for stock_signal_transition_analytics upserts.
-- Until applied, Titan omits the column on upsert and persists other transition fields.

alter table if exists public.stock_signal_transition_analytics
    add column if not exists accumulate_signal_consistency_ratio double precision not null default 0;

comment on column public.stock_signal_transition_analytics.accumulate_signal_consistency_ratio is
    'Share of trailing-window action_signal labels that were accumulate (0–1).';
