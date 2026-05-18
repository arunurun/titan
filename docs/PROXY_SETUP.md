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
   - `BREEZE_API_KEY` (optional but required for **Open Breeze login**: `GET /breeze-login` redirects to ICICI using this key; without it the endpoint responds with `501` and a JSON error)
4. Deploy Worker.
5. Copy Worker URL, e.g. `https://titan-dispatch.<subdomain>.workers.dev`
6. Set that Worker URL as `PROXY_BASE` in `docs/app.js`.
7. Deploy/update the UI static site.
8. Tap **Test Connection** in the UI. It should return `Connection OK` with repo/workflow details.

## Breeze login redirect

- `GET https://<your-proxy>/breeze-login` → `302` to ICICI Breeze login (uses `BREEZE_API_KEY` secret).
- Trailing slashes are accepted (`/breeze-login/`).
- Set the secret from the repo root: `npx wrangler secret put BREEZE_API_KEY` (uses root `wrangler.toml` / `titan-proxy` worker).

## Common UI errors and fixes

- `404 Not Found`
  - Cause: `PROXY_BASE` points to a URL that is not the backend Worker API.
  - Fix: set `PROXY_BASE` to the Worker base URL that exposes `/health`, `/dispatch`, `/runs`, then redeploy UI.
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

## Security notes

- Keep `GITHUB_PAT` only in Worker secrets.
- Never expose PAT in `docs/` files.
- Optional hardening:
  - Add simple shared passcode header
  - Restrict CORS to your Pages domain
  - Add rate limiting in Worker
