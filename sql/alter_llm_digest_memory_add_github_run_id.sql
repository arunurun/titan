-- Links llm_digest_memory rows to GitHub Actions workflow runs (for Titan Mobile /insights/github-run/…).
-- Run once in Supabase SQL Editor after upgrading Titan + the Run Titan Now workflow.

alter table if exists public.llm_digest_memory
    add column if not exists github_run_id text;

comment on column public.llm_digest_memory.github_run_id is
    'GitHub Actions run id (workflow_dispatch) when digest was persisted from CI; enables UI lookup by run.';

create index if not exists idx_llm_digest_memory_github_sector
    on public.llm_digest_memory (github_run_id, sector)
    where github_run_id is not null;
