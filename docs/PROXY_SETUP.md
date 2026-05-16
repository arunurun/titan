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
4. Deploy Worker.
5. Copy Worker URL, e.g. `https://titan-dispatch.<subdomain>.workers.dev`
6. Open Pages UI and set **Dispatch Proxy URL** to that Worker URL once.
7. Tap **Test Connection** in the UI. It should return `Connection OK` with repo/workflow details.

## Common UI errors and fixes

- `404 Not Found`
  - Cause: Dispatch Proxy URL points to the UI page/static host, not the backend Worker API.
  - Fix: Set **Dispatch Proxy URL** to the Worker base URL that exposes `/health`, `/dispatch`, `/runs`.
- `401 Auth/permission error from GitHub`
  - Cause: invalid/expired `GITHUB_PAT` in Worker secrets.
  - Fix: rotate `GITHUB_PAT` in Cloudflare Worker secrets.
- `403 Auth/permission error from GitHub`
  - Cause: token lacks required repo permissions or repo access.
  - Fix: update PAT permissions (Actions write, Contents read/write, Metadata read) and repo selection.
- `Proxy URL is invalid` / `Proxy URL is required`
  - Cause: malformed or empty URL.
  - Fix: use full URL format like `https://your-worker.workers.dev`.

The UI now auto-normalizes pasted values like `/health`, `/dispatch`, or `/runs` to the proxy base URL.

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
