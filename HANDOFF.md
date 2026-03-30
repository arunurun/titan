# Resume: GitHub Actions inject + session token

## Implemented (2026)

- **Repository secret `BREEZE_SESSION_TOKEN` (optional):** If set, `scripts/inject_breeze_session_from_supabase.py` uses it and **skips** reading Supabase `session_config`. Paste the daily Breeze token in GitHub Secrets when you prefer not to rely on the table.
- **Otherwise:** Same as before — read from `session_config` using `SUPABASE_URL` + **service_role** `SUPABASE_KEY`.

Workflow step: **Load Breeze session token (secret or Supabase)** passes `BREEZE_SESSION_TOKEN: ${{ secrets.BREEZE_SESSION_TOKEN }}`.

## If CI still fails

1. **Quick path:** GitHub → Settings → Secrets → add **`BREEZE_SESSION_TOKEN`** = same value as local `.env` `BREEZE_SESSION_TOKEN`.
2. **Supabase path:** SQL `sql/create_session_config.sql` (or `ensure_session_config_row.sql`), Table Editor non-empty token, **`SUPABASE_KEY` = service_role** (not anon).

## Verify locally

```powershell
cd C:\Arun\Study\Cursor\Titan
python -m pytest tests\ -q --ignore=tests\test_config_loader.py
```

## Trigger CI

- Actions → **Market audit (Titan V12.0)** → **Run workflow**
