# Titan — build, deploy, and change process

This document is for humans and AI agents working in this repository. It describes how changes flow to **GitHub**, **Supabase**, **Cloudflare** (Wrangler), and the **Android TWA** app.

## Repository layout (what touches what)

| Area | Path | What it is |
|------|------|------------|
| Core Python app | `src/`, `main.py`, `tests/` | Titan scanner / analysis |
| Mobile control UI | `docs/` (`index.html`, `app.js`) | Static web UI; talks to the proxy Worker |
| GitHub dispatch proxy | `proxy/cloudflare-worker.js` | Worker: Actions dispatch, runs, optional Supabase insights |
| Wrangler config (proxy) | `wrangler.toml` (repo root) | Deploys the **API** Worker (`titan-proxy` by default) |
| Android TWA | `android/twa/` | Trusted Web Activity shell around the hosted UI URL |
| Digital Asset Links | `docs/.well-known/assetlinks.json` | Must be served from the **same origin** as the TWA URL |
| Supabase DDL / fixes | `sql/*.sql` | Run in Supabase SQL Editor (or your migration pipeline) |
| Universe → Supabase | `provider-universe-sync/` | Weekly ICICI scrip sync to `market_instruments` / sectors |

There are effectively **two Cloudflare surfaces**:

1. **Proxy API Worker** — configured by root `wrangler.toml` (`main = "proxy/cloudflare-worker.js"`). The UI uses `PROXY_BASE` in `docs/app.js` (must be this Worker’s origin, not GitHub Pages HTML).
2. **Static UI** — same `docs/` tree, often deployed as a separate Worker **with static assets** (example from `android/twa/README.md`: `wrangler deploy --name titan-ui --assets ./docs`). The Android app’s `twa_default_url` in `android/twa/app/src/main/res/values/strings.xml` must match this **origin**.

---

## 1. GitHub

### Branches

- **`main`** — primary integration branch for the Python app, workflows, and shared `docs/`.
- **`android`** — preferred branch for **TWA-only** or Android-adjacent work (keeps experiments off `main` until you merge). The workflow `.github/workflows/android-twa-apk.yml` builds a debug APK on pushes to `android` when `android/twa/**` changes.

### Pushing changes

1. Commit on the appropriate branch (`android` for TWA-focused work, or `main` / feature branch as your team prefers).
2. Push to `origin`:

   ```bash
   git push -u origin HEAD
   ```

3. Open a **pull request** into `main` when the change should become the long-lived default (use `gh pr create` if you use GitHub CLI).

### Actions and secrets

- Workflows live under `.github/workflows/`.
- Repository **secrets** (for example Breeze token, Supabase keys used in CI) are configured in GitHub: **Settings → Secrets and variables → Actions**. Do not commit secrets into the repo.
- Manual runs (for example `run_titan_now.yml`, `breakout_scan.yml`) are triggered from the Actions tab or from the mobile UI via the proxy.

---

## 2. Supabase

This project does not ship a generated Supabase CLI migration tree; schema and one-off changes live as **SQL files** under `sql/`.

### Applying schema or data changes

1. Open the Supabase project **SQL Editor**.
2. Run the relevant script (for example `sql/create_sector_registry_tables.sql`, or any `sql/*.sql` added for a feature).
3. Confirm **RLS policies** and keys: app and scripts typically need **`SUPABASE_URL`** and a key with sufficient privilege (**service role** only on servers/Workers, never in the browser or in `docs/`).

### Related automation

- **Sector / instrument registry sync**: `provider-universe-sync/` (see its README and GitHub workflow under `provider-universe-sync/.github/workflows/`).
- **Worker “insights”**: optional Worker routes read Supabase (see `docs/PROXY_SETUP.md`); requires Worker secrets `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

After SQL changes, redeploy any component that caches assumptions (usually not required for pure DDL, but document new tables/columns in PR description).

---

## 3. Cloudflare (Wrangler)

Install Wrangler locally (from repo root):

```bash
npm install
# or use without install:
npx wrangler --version
```

Log in once per machine:

```bash
npx wrangler login
```

### 3a. Deploy the **proxy** Worker (API)

Root `wrangler.toml` defines the **dispatch** Worker (name defaults to `titan-proxy` in examples; your `name` in `wrangler.toml` is the source of truth).

```bash
cd /path/to/Titan
npx wrangler deploy
```

Set **secrets** (not in `wrangler.toml`; use Wrangler or the Cloudflare dashboard):

- `GITHUB_PAT` — fine-grained PAT with Actions dispatch permissions for this repo.
- Optional: `BREEZE_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (see `docs/PROXY_SETUP.md`).

Example (repo root, uses `wrangler.toml`):

```bash
npx wrangler secret put GITHUB_PAT
```

`[vars]` in `wrangler.toml` (for example `REPO_OWNER`, `REPO_NAME`) are non-secret and ship with the Worker; adjust for your fork.

### 3b. Deploy the **static UI** (docs site)

The TWA loads a **full URL** (`twa_default_url`). That URL’s origin must serve:

- The UI (`index.html`, `app.js`, …)
- `/.well-known/assetlinks.json` for Digital Asset Links

Deploy `docs/` as your team’s static hosting allows. If you use a **second** Worker with static assets (as in `android/twa/README.md`):

```bash
cd /path/to/Titan
npx wrangler deploy --name titan-ui --assets ./docs
```

Use a Worker name and route consistent with `strings.xml` and `assetlinks.json` hosting.

### 3c. After changing the UI or proxy

1. Edit `docs/app.js` (for example `PROXY_BASE`) or `proxy/cloudflare-worker.js` as needed.
2. Redeploy **proxy** if Worker code or secrets changed.
3. Redeploy **static UI** if `docs/` changed.
4. If the **public origin** of the UI changed, update **`twa_default_url`** in `android/twa/app/src/main/res/values/strings.xml` and fix **`docs/.well-known/assetlinks.json`**, then redeploy the static site.

---

## 4. Android app (TWA)

### Where to work

- Project root for Gradle: **`android/twa/`** (open this folder in Android Studio unless you prefer the whole monorepo).

### Local build

- JDK **17**, Android Studio, Android **34** SDK (matches CI).
- Debug build:

  ```bash
  cd android/twa
  chmod +x ./gradlew   # Unix; on Windows use gradlew.bat
  ./gradlew assembleDebug
  ```

### Release / Play

- **Build → Generate Signed Bundle / APK** in Android Studio; use **AAB** for Play Console.
- Update **`docs/.well-known/assetlinks.json`** with the **SHA-256** of the key that signs the build users install (upload key or Play App Signing key), and redeploy the **same origin** as `twa_default_url`.

### CI

- Workflow: `.github/workflows/android-twa-apk.yml` — uploads **`titan-twa-debug-apk`** artifact on qualifying pushes/PRs.

More detail: **`android/twa/README.md`**.

---

## 5. Typical end-to-end order (cross-cutting change)

Example: you change the control UI and need the app + proxy aligned.

1. Edit `docs/` (and `docs/app.js` / `PROXY_BASE` if the proxy URL changes).
2. Edit `proxy/cloudflare-worker.js` if the API contract changes.
3. **`npx wrangler deploy`** for the proxy Worker.
4. Deploy static **`docs/`** to the UI origin (Wrangler assets or Pages).
5. If the UI **origin** changed: update `strings.xml` → `twa_default_url`, update `assetlinks.json`, redeploy static site, rebuild Android.
6. **`git push`** on the correct branch; merge via PR if required.
7. Supabase: run any new **`sql/`** scripts if the feature needs database changes.

---

## 6. Tests (Python)

From repository root (see `pytest.ini`):

```bash
python -m pytest tests -q
```

Provider package tests (equity filter, etc.):

```bash
cd provider-universe-sync
python -m pytest tests -q
```

---

## 7. Safety checklist for agents

- Never commit **PATs**, **service role** keys, or `.env` with real secrets.
- **`PROXY_BASE`** must point at the **Worker API root** (returns JSON for `/health`), not a generic Pages URL that serves unrelated HTML.
- **`twa_default_url`** and **`assetlinks.json`** must share the **same origin** for TWA verification.
- Prefer **`android`** branch for TWA-only churn; merge to **`main`** when stable.
