-- Run in Supabase SQL Editor if insert fails with:
--   "new row violates row-level security policy for table audit_logs"
--
-- Preferred fix: use the service_role key as SUPABASE_KEY (Settings → API → service_role secret).
-- That JWT bypasses RLS for server-side scripts.
--
-- If you must keep using the anon key, use Option B instead.

-- Option A — backend-only table: disable RLS (simplest for Titan CLI / --live)
alter table public.audit_logs disable row level security;

-- Option B — keep RLS on and allow inserts via PostgREST (e.g. anon key)
-- alter table public.audit_logs enable row level security;
-- drop policy if exists "audit_logs_insert" on public.audit_logs;
-- create policy "audit_logs_insert"
--   on public.audit_logs
--   for insert
--   to anon, authenticated
--   with check (true);
