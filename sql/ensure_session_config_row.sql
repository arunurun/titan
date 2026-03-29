-- Run in Supabase SQL Editor if GitHub Actions inject step finds no rows.
-- Safe if the table already exists (from create_session_config.sql).

insert into public.session_config (id, breeze_session_token)
values (1, '')
on conflict (id) do update set updated_at = now();
