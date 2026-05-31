-- Run once in Supabase SQL Editor. Stores the daily Breeze session token for CI.
-- Update the row each morning before the scheduled job (e.g. paste token in Table Editor).

create table if not exists public.session_config (
  id smallint primary key default 1 check (id = 1),
  breeze_session_token text not null default '',
  updated_at timestamptz not null default now()
);

-- Ensure row id=1 exists (re-run safe)
insert into public.session_config (id, breeze_session_token)
values (1, '')
on conflict (id) do update set updated_at = now();

comment on table public.session_config is
  'TITAN Breeze session token for CI (id=1 row). News/CI secrets live in public.titan_secrets.';

-- Service role (used in Actions) bypasses RLS; anon should not read this table.
alter table public.session_config enable row level security;

-- No policies for anon/authenticated: only service_role can access (recommended for secrets).
