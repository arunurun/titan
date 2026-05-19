-- If GitHub Actions uses the Supabase *anon* key as SUPABASE_KEY, PostgREST needs table grants.
-- Prefer setting SUPABASE_KEY to the service_role key (same project as SUPABASE_SERVICE_ROLE_KEY on the Worker).

grant usage on schema public to anon, authenticated, service_role;

grant select, insert, update, delete on table public.llm_digest_memory to service_role;
grant select, insert, update on table public.llm_digest_memory to anon, authenticated;

grant select, insert, update, delete on table public.run_metadata to service_role;
grant select, insert, update on table public.run_metadata to anon, authenticated;

grant select, insert, update, delete on table public.symbol_daily_features to service_role;
grant select, insert, update, delete on table public.sector_daily_rollup to service_role;
grant select, insert, update, delete on table public.sector_period_rollup to service_role;
