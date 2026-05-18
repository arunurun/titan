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

---

## Work log — 2026-05-18

### GitHub Actions: “Run Titan Now” looked stuck

**Symptom:** Workflow **Run Titan Now** (`run_titan_now.yml`), job **`run`**, stayed **in progress** on step **Run Titan** (`python main.py …`) for a long time (e.g. 26+ minutes) with no failed-step logs until the job finished or was cancelled.

**Findings:**

- The **`run`** job had **no `timeout-minutes`**, so a hung or very slow step could run until the **hosted runner default** (on the order of hours), which feels stuck.
- Likely stall points inside **`main.py`**: **Breeze** (`get_historical_data` and related SDK calls often have **no explicit socket timeout**), **Gemini**, or **SMTP** without connect/send timeout.
- **`src/breeze_client.py`** uses a **process-wide `_HIST_CALL_LOCK`** around historical fetches plus a minimum interval between calls. All parallel sector/symbol work **queues on that lock**, so **`all_sectors`** with high **`all_sector_workers`** adds **thread contention** without speeding Breeze historical I/O.

**Changes made (repo):**

- **`.github/workflows/run_titan_now.yml`:** `timeout-minutes: 180` on job **`run`**; default **`all_sector_workers`** input **20 → 8**.
- **`src/email_notify.py`:** **`SMTP(..., timeout=…)`** / **`SMTP_SSL(..., timeout=…)`** via **`SMTP_TIMEOUT_SECONDS`** (default **60**).
- **`config/.env.example`:** documents **`SMTP_TIMEOUT_SECONDS`**.

**Follow-up (if logs still implicate Breeze):** consider bounded timeouts or isolation around **`get_historical_data`** (careful with thread safety and orphaned calls).

### Gemini: sector digest failed with 429 (free tier daily quota)

**Symptom:** Failure email: **`[Gemini] 429 RESOURCE_EXHAUSTED`**, quota **`generativelanguage.googleapis.com/generate_content_free_tier_requests`**, **`GenerateRequestsPerDayPerProjectPerModel-FreeTier`**, **`quotaValue: 20`**, model **`gemini-2.5-flash-lite`** (example stack: `generate_sector_digest_narrative` → `generate_titan_narrative` → `_generate` in **`src/brain.py`**).

**Findings:**

- **Free tier** caps **requests per day per project per model** (here **20** for that model). **Retrying with backoff does not reset a daily cap**; the old loop could waste time before still failing the audit.
- **`GEMINI_API_KEY_2`** only helps if the second key is a **different Google Cloud project** with its own quota; two keys on the **same** project share the same daily pool.
- **`GEMINI_COMPLIANCE_RETRY=false`** saves **one** extra API call when the first draft fails the wording policy (optional for quota savings).

**Changes made (repo):**

- **`src/brain.py`:** **`_is_per_day_quota_exhausted()`**; **`_generate()`** **stops the backoff loop immediately** when the error indicates **per-day** quota and there is **no next key** to try.
- **`fallback_sector_digest_narrative()`** and **`generate_sector_digest_narrative()`:** if **`GEMINI_SECTOR_DIGEST_FAIL_OPEN`** is **true** (default), **Gemini / policy failures** for the sector digest use a **deterministic metrics snapshot** so the run can still **complete and email** instead of failing the whole job. Set **`GEMINI_SECTOR_DIGEST_FAIL_OPEN=false`** to require LLM text (fail hard).
- **`config/.env.example`:** documents **`GEMINI_SECTOR_DIGEST_FAIL_OPEN`**.
- **`tests/test_brain.py`:** covers fast-fail on daily quota and digest fail-open behavior.

**Operational mitigations (outside code):** enable **billing** / higher quota for the Gemini project, or set **`GEMINI_MODEL`** to a model with a **separate** quota line; see [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).
