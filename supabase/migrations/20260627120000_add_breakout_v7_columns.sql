-- Add v7 evidence / ranking columns to breakout_stock_analysis.
-- Safe when table was created from an older CREATE TABLE (IF NOT EXISTS skips new columns).
-- Idempotent: ADD COLUMN IF NOT EXISTS is a no-op when columns already exist.

alter table public.breakout_stock_analysis
    add column if not exists signal_tier text,
    add column if not exists persistence_score integer,
    add column if not exists composite_rank double precision,
    add column if not exists liquidity_quality double precision,
    add column if not exists breakout_stage integer,
    add column if not exists base_score double precision,
    add column if not exists pass_paths text,
    add column if not exists risk_flags text;
