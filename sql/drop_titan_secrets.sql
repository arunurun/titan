-- Optional: drop titan_secrets only if consolidating elsewhere (not recommended while news pipeline uses it).
-- Verify row count before drop: select key_name, length(value) from public.titan_secrets;

drop table if exists public.titan_secrets;
