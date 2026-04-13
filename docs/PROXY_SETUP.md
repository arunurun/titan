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
