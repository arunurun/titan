# Titan Mobile Control - Tokenless Setup

This page (`docs/index.html`) is now tokenless for end users.
It requires a small backend proxy that stores GitHub PAT server-side.

## Fast setup with Cloudflare Worker

1. Create a Cloudflare Worker.
2. Paste `proxy/cloudflare-worker.js` as worker code.
3. Add Worker secrets:
   - `GITHUB_PAT` (fine-grained token with repo Actions write access)
   - `REPO_OWNER` = `arunurun`
   - `REPO_NAME` = `titan`
   - `BREEZE_API_KEY` (optional but required for **Open Breeze login**: `GET`/`HEAD` `/breeze-login` redirects to ICICI using this key; without it the endpoint responds with `501` and a JSON error).  
  Upload from a machine with Wrangler logged in and `.env` present:
  `python scripts/emit_breeze_api_key_for_wrangler.py | npx wrangler secret put BREEZE_API_KEY`
   - `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (optional; required for **Sector insight in app** — `GET /insights/latest?sector=…` reads `public.llm_digest_memory`. Use the **service role** key only in the Worker, never in the browser.)

### Supabase secrets (Wrangler, from repo root)

These attach to the **titan-proxy** worker (`wrangler.toml`):

```bash
npx wrangler secret put SUPABASE_URL
# paste your project URL, e.g. https://abcdefgh.supabase.co

npx wrangler secret put SUPABASE_SERVICE_ROLE_KEY
# paste the service_role key from Supabase → Project Settings → API
```

Then redeploy: `npx wrangler deploy`. In the UI, **Test connection** should show **Supabase insights: yes**.
4. Deploy Worker.
5. Copy Worker URL, e.g. `https://titan-dispatch.<subdomain>.workers.dev`
6. Set that Worker URL as `PROXY_BASE` in `docs/app.js`.
7. Deploy/update the UI static site.
8. Tap **Test Connection** in the UI. It should return `Connection OK` with repo/workflow details and **`Supabase insights: yes`** once `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set on the Worker.

## Sector insight (TWA / mobile UI)

- After a **sector** or **custom** digest run completes in GitHub Actions, Titan writes the full digest into Supabase `llm_digest_memory.full_digest` (and the short LLM narrative in `output_text`).
- Run `sql/alter_llm_digest_memory_add_full_digest.sql` once if your project created `llm_digest_memory` before that column existed.
- The Worker exposes **`GET /insights/latest?sector=<sector_id>`** (same `sector_id` pattern as dispatch). The static UI loads it into **Sector insight in app**.

## Breeze login redirect

- `GET` or `HEAD` `https://<your-proxy>/breeze-login` → `302` to ICICI Breeze login (uses `BREEZE_API_KEY` secret). (`curl -I` sends `HEAD`; the worker accepts both.)
- Trailing slashes are accepted (`/breeze-login/`).
- Set the secret from the repo root: `npx wrangler secret put BREEZE_API_KEY` (uses root `wrangler.toml` / `titan-proxy` worker).

## Common UI errors and fixes

- `404 Not Found`
  - Cause: `PROXY_BASE` points to a URL that is not the backend Worker API.
  - Fix: set `PROXY_BASE` to the Worker base URL that exposes `/health`, `/dispatch`, `/runs`, `/workflow-run/{id}`, `/insights/latest`, then redeploy UI.
- `401 Auth/permission error from GitHub`
  - Cause: invalid/expired `GITHUB_PAT` in Worker secrets.
  - Fix: rotate `GITHUB_PAT` in Cloudflare Worker secrets.
- `403 Auth/permission error from GitHub`
  - Cause: token lacks required repo permissions or repo access.
  - Fix: update PAT permissions (Actions write, Contents read/write, Metadata read) and repo selection.
- `Proxy URL is invalid`
  - Cause: malformed `PROXY_BASE` value.
  - Fix: use full URL format like `https://your-worker.workers.dev`.

The UI uses a hardcoded `PROXY_BASE` constant and no longer requires manual proxy URL entry.

## API contract used by UI

- `POST /dispatch`
  - body: `{ "workflow": "<filename>", "ref": "main", "inputs": { ... } }`
- `GET /runs?limit=20`
  - returns GitHub workflow runs payload
- `GET /workflow-run/{id}`
  - returns a small JSON view of one GitHub Actions run (including `inputs` for workflow_dispatch), used to align a completed run with a Supabase sector digest
- `GET /insights/latest?sector=<id>`
  - returns `{ "ok": true, "insight": null }` or `{ "ok": true, "insight": { "run_id", "sector", "recorded_at", "text" } }`
  - requires Worker secrets `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`

## Security notes

- Keep `GITHUB_PAT` only in Worker secrets.
- Never expose PAT in `docs/` files.
- Optional hardening:
  - Add simple shared passcode header
  - Restrict CORS to your Pages domain
  - Add rate limiting in Worker
