-- Run once in Supabase: SQL Editor → New query → Paste → Run.
-- Matches save_audit_log() in src/supabase_log.py (audit, post, recorded_at_ist).

create table if not exists public.audit_logs (
  id bigint generated always as identity primary key,
  audit jsonb not null,
  post text,
  recorded_at_ist text not null
);

-- Avoid RLS blocking inserts when using anon key; prefer service_role in .env for production.
alter table public.audit_logs disable row level security;
