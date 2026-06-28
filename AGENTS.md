# AGENTS.md

## Cursor Cloud specific instructions

Titan is a Python 3 market-analysis engine (NIFTY / sectors / options / breakout / news)
with a few satellite surfaces. Most surfaces need live broker/cloud credentials; the
parts that run with **no credentials** are the CLI engine `--dry-run` and the pytest suite.

### Branch guardrail
- `.cursorrules` forbids modifying code while on `main`. Always work on a feature branch.

### Dependencies / environment
- Python deps install to the user site via `pip install` (no virtualenv needed); the startup
  update script already runs `pip install -r requirements.txt` plus the subproject requirements.
- **Gotcha (do not remove the pandas/numpy pin):** `requirements.txt` is unpinned
  (`pandas>=2.0.0`, `numpy>=1.24.0`). The latest resolutions (pandas 3.x + numpy 2.4) **segfault**
  the test suite in `pandas.date_range` (e.g. `tests/test_tape_metrics.py`). The update script
  therefore force-installs stable `pandas>=2.0,<2.3` / `numpy>=1.24,<2.1`. If you manually
  reinstall/upgrade pandas or numpy, re-apply that pin or tests will crash with a native fault.

### Run the app (core functionality)
- No-credential smoke run: `python3 main.py --dry-run` → prints a computed audit
  (z_score, absorption_ratio, pcr, oi_wall, intent_score). This is the canonical
  end-to-end validation path without external services.
- Live / sector / all-sector runs (`python3 main.py --live|--sector <id>|--all-sectors`)
  need `.env` with Breeze (`BREEZE_API_KEY/SECRET/SESSION_TOKEN`), Gemini (`GEMINI_API_KEY[S]`),
  Supabase (`SUPABASE_URL/KEY`), and SMTP for digests. See `config/.env.example`. CLI flags: `python3 main.py --help`.

### Control UI (optional service)
- `python3 control_ui/app.py` serves the Flask operator panel on `http://127.0.0.1:8787`
  (override port with `TITAN_UI_PORT`). The page renders without credentials, but every
  action button (Run Analysis, Reconcile, Find Breakouts, Validate/Persist Token, Portfolio)
  drives the engine and requires the live Breeze/Supabase/Gemini creds above.

### Tests & lint
- Run from repo root: `pytest` (mirrors CI `pytest --maxfail=2 --disable-warnings`).
  Full suite is ~3 min and 700+ tests; some tests make **real network calls** so they are slow
  but pass. `integration` / `breeze_live` markers auto-skip without credentials.
- Subproject: `cd provider-universe-sync && python3 -m pytest tests -q`.
- No linter is configured (CI runs only pytest). `python3 -m compileall main.py src control_ui scripts`
  is a quick syntax check.

### Other surfaces (not needed to run/test the core engine)
- `proxy/` + `wrangler.toml` (Cloudflare Worker) and `docs/` static UI: deploy with `npx wrangler`
  after `npm install` (root `package.json` only pins wrangler). `android/twa/` is a Gradle/JDK17 build.
  `supabase/`+`sql/` are applied manually in the Supabase SQL Editor. See `docs/TITAN_BUILD_AND_DEPLOY.md`.
