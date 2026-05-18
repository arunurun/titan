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

## Manual token validator alert

`Validate Breeze Token (Manual)` now sends an optional action-required email when token validation fails (missing row/token or invalid/expired token). The email includes a clickable **Login to Breeze** link.

Set these repository secrets to enable email alerts for that workflow:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `EMAIL_FROM`, `EMAIL_TO`
- Optional: `SMTP_USE_TLS`, `TOKEN_UPDATE_URL`

## Schedule (GitHub)

Workflow runs **weekdays** during **09:15–15:30 IST** (crons are **UTC**; see `market_audit.yml`). Includes **11:00 IST** and **11:15 IST** slots. ICICI session tokens still expire independently.

### If a scheduled run does not appear

1. **GitHub often delays** scheduled jobs (sometimes **15–60+ minutes**); see [schedule event](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule).
2. **Default branch** must be the branch that contains `.github/workflows/market_audit.yml` (usually `main`).
3. **Settings → Actions → General**: Actions must be allowed; workflow must not be disabled under **Actions** tab.
4. **Forks**: scheduled workflows **do not run** on forks (only on the upstream repo you own).
5. After changing `schedule`, allow **up to ~1 hour** for the scheduler to pick up new crons.
6. In **Actions**, filter runs by **event = schedule** (not only `workflow_dispatch`).

## Automating Breeze session refresh?

**Fully automatic refresh is not supported by ICICI in a safe, ToS-compliant way for this project.** Breeze issues `API_Session` only after **browser login** (often **OTP**). There is no documented “refresh token” you can call from GitHub Actions with only `BREEZE_API_KEY` + `BREEZE_SECRET`.

**Practical options:**

| Approach | Notes |
|----------|--------|
| **Manual** | Run `python scripts/breeze_session.py`, then paste the token into GitHub **Secrets** and/or **Supabase `session_config`**. |
| **Semi-automated** | A **local scheduled task** (Windows Task Scheduler) that opens the script each morning; you complete OTP once, then copy the token to GitHub/Supabase. |
| **Not recommended** | Headless browser + stored banking credentials: brittle, high risk, may violate ICICI terms. |

If ICICI ever documents an unattended session API, you could wire a small job to update `session_config`—until then, keep the human-in-the-loop step.

## Optional: email after each successful `--live`

Set `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (comma-separated). Same post text as Supabase. In GitHub, add matching **repository secrets** (see `market_audit.yml` `Live market audit` env).

## Optional: all-sector consolidated email

Default `--all-sectors` behavior: Titan sends **one** consolidated success email after every sector finishes (same digest content as before, concatenated under `=== Sector: … ===` headers).

To restore **one email per sector** instead, set:

- `TITAN_ALL_SECTORS_SINGLE_DIGEST=0`

## Verify

```powershell
cd C:\Arun\Study\Cursor\Titan
python -m pytest tests\ -q --ignore=tests\test_config_loader.py
```
