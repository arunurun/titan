# CI: Breeze session token inject

## How it works (after latest fix)

1. **GitHub Actions** step **Load Breeze session token** runs **bash first**:
   - If repository secret **`BREEZE_SESSION_TOKEN`** is non-empty → writes `GITHUB_ENV` (no Supabase call).
   - Else runs **`python scripts/inject_breeze_session_from_supabase.py`** (reads `session_config`).

2. **`SUPABASE_KEY`** must be **service_role** JWT if using the Supabase path (anon → empty rows under RLS). The script now **detects anon** and fails with a clear message.

## Fix “session_config returned no rows”

**Fastest:** GitHub → **Settings → Secrets and variables → Actions** → create **`BREEZE_SESSION_TOKEN`** = same value as local `.env` `BREEZE_SESSION_TOKEN` (update daily).

**Or:** Supabase SQL `sql/create_session_config.sql` + Table Editor non-empty token + **`SUPABASE_KEY` = service_role** (not anon).

## “Session key is expired” (Breeze)

ICICI session tokens are short-lived. Before each scheduled run (or when this appears):

1. Log in via browser / run `python scripts/breeze_session.py` to refresh.
2. Update **GitHub secret `BREEZE_SESSION_TOKEN`**, local **`.env`**, and **Supabase `session_config`** so they all match the new token.

## Verify

```powershell
cd C:\Arun\Study\Cursor\Titan
python -m pytest tests\ -q --ignore=tests\test_config_loader.py
```
