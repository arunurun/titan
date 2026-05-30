-- DEPRECATED: News secrets belong in public.titan_secrets only.
-- Use sql/restore_session_config_from_kv.sql to revert if this migration was applied.
-- Extend session_config for news/CI key-value rows (consolidates former public.titan_secrets).
-- Breeze token stays on row id=1 (breeze_session_token column); KV rows use key_name/value.
-- Run once in Supabase SQL Editor or via scripts/apply_session_config_secrets.py.

alter table public.session_config drop constraint if exists session_config_id_check;

alter table public.session_config add column if not exists key_name text;
alter table public.session_config add column if not exists value text not null default '';
alter table public.session_config add column if not exists description text;

create unique index if not exists session_config_key_name_uidx
  on public.session_config (key_name)
  where key_name is not null;

insert into public.session_config (id, breeze_session_token)
values (1, '')
on conflict (id) do nothing;

comment on table public.session_config is
  'TITAN config: id=1 row holds breeze_session_token; additional rows use key_name/value for news/CI secrets.';

-- Migrate data from deprecated titan_secrets (safe to re-run; skips if source table missing).
do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'titan_secrets'
  ) then
    insert into public.session_config (id, key_name, value, updated_at, description)
    select
      (select coalesce(max(s.id), 1) from public.session_config s) + row_number() over (order by t.key_name),
      t.key_name,
      t.value,
      t.updated_at,
      t.description
    from public.titan_secrets t
    where t.key_name is not null
    on conflict (key_name) where key_name is not null do update
      set value = excluded.value,
          updated_at = excluded.updated_at,
          description = coalesce(excluded.description, public.session_config.description);
  end if;
end $$;
