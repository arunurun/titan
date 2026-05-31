-- Server-side news/CI secrets (NEWSAPI_API_KEY, SUPABASE_URL, etc.).
-- Run once in Supabase SQL Editor or via scripts/apply_titan_secrets_migration.py.
create table if not exists public.titan_secrets (
    key_name text primary key,
    value text not null default '',
    updated_at timestamptz not null default now(),
    description text
);

alter table public.titan_secrets add column if not exists description text;

-- Allow uppercase env-style key names (e.g. NEWSAPI_API_KEY).
alter table public.titan_secrets drop constraint if exists titan_secrets_key_name_lower;

comment on table public.titan_secrets is
    'Server-side config/secrets (NEWSAPI_API_KEY, SUPABASE_URL, etc.). Update via service_role; not exposed to anon.';

alter table public.titan_secrets enable row level security;

-- No policies for anon/authenticated: only service_role can read/write.
revoke all on table public.titan_secrets from anon, authenticated;
revoke all on table public.titan_secrets from public;
