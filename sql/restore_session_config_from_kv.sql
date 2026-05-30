-- Revert session_config KV merge (see sql/alter_session_config_add_kv.sql — deprecated).
-- Keeps id=1 Breeze row; removes news KV rows and drops KV columns.
-- Run via scripts/restore_session_config_from_kv.py or Supabase SQL Editor.

delete from public.session_config where id <> 1;

update public.session_config
set key_name = null, value = '', description = null
where id = 1 and key_name is not null;

drop index if exists public.session_config_key_name_uidx;

alter table public.session_config drop constraint if exists session_config_id_check;
alter table public.session_config add constraint session_config_id_check check (id = 1);

alter table public.session_config drop column if exists key_name;
alter table public.session_config drop column if exists value;
alter table public.session_config drop column if exists description;

comment on table public.session_config is
  'TITAN Breeze session token for CI (id=1 row). News/CI secrets live in public.titan_secrets.';
