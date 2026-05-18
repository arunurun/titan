-- Optional: full sector/custom digest (email-equivalent body) for Titan Mobile UI / TWA.
-- Run in Supabase SQL Editor if you use GET /insights/latest on the dispatch Worker.

alter table if exists public.llm_digest_memory
    add column if not exists full_digest text;

comment on column public.llm_digest_memory.full_digest is
    'Full plaintext digest for sector/custom runs (matches email body when persisted).';
