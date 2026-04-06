# Session handoff — 6 Apr 2026 (Titan / `sector` branch)

Conversation summary for resuming work. Branch: **`sector`**.

---

## What we built / fixed (chronological themes)

1. **Gemini free tier vs sector size**  
   - Sector **digest**: one `generate_content` for the whole sector after per-symbol Breeze metrics.  
   - Added **`--sector-max-symbols N`**, **`--sector-workers N`**.  
   - CI (`market_audit.yml` on `sector`) runs sector defence with **default digest** (no extra flag). Optional **`GEMINI_API_KEY_2`**.

2. **Quota optimization (later commit)**  
   - **`python main.py --sector <id>` defaults to digest mode** (~1 Gemini call per run).  
   - **`--sector-per-symbol-narrative`** = old behaviour (one call per symbol, high quota).  
   - **`--sector-digest`** kept hidden / backward compatible (no-op; digest is default).  
   - **`run_sector_live(..., digest=True)`** default in code.  
   - **`GEMINI_COMPLIANCE_RETRY=false`**: skip second Gemini call when first draft fails compliance (saves 1 call on that path).  
   - **`GEMINI_COMPACT_PROMPT`**: default **true** — minified JSON in prompts (fewer tokens; set `false` to debug).

3. **Multiple API keys**  
   - Env: `GEMINI_API_KEY`, `GEMINI_API_KEY_2`…`GEMINI_API_KEY_5`, or `GEMINI_API_KEYS` (comma-separated).  
   - **`brain._generate`**: on 429 / transient errors, try next key; backoff on last key.

4. **503 “high demand”**  
   - Treated as retryable (same path as quota).

5. **NaN in audit JSON**  
   - Added **`src/json_util.py`** → **`sanitize_for_json`**.  
   - Used in **`brain`** (Gemini prompt) and **`supabase_log`** (insert).

6. **Rotation robustness**  
   - **`google.genai.errors.APIError`** / **`.code`**, **`_is_retryable_gemini_exception`**, log: `trying next API key`.

7. **Sector / Breeze**  
   - ICICI scrip map; some BSE names return empty history → `No rows returned`.  
   - Historical window: **~60 calendar days** ending “now”; logs show **February** at the **start** of the array, **April** at the end — not “wrong month”.

8. **Live test runs**  
   - Smoke with **`--sector-max-symbols 3`**: Breeze → Gemini → Supabase → email.  
   - Full runs can hit **429** when **daily** free-tier cap exhausted on both keys.

---

## Commands (reference)

```bash
# Default: one digest narrative per sector run (~1 Gemini call)
python main.py --sector defence

# Per-symbol narratives (many Gemini calls)
python main.py --sector defence --sector-per-symbol-narrative

# Smoke
python main.py --sector defence --sector-max-symbols 3

# NIFTY live (separate path; 1 narrative)
python main.py --live
```

Tests: `python -m pytest`.

---

## Env flags (quota / prompts)

| Variable | Effect |
|----------|--------|
| `GEMINI_COMPLIANCE_RETRY=false` | No repair call after compliance fail on first draft |
| `GEMINI_COMPACT_PROMPT=false` | Pretty-print JSON in prompts (debug) |

See `config/.env.example`.

---

## Next session (suggestions)

- Confirm **two distinct** Google AI projects/keys if both hit 429 same day.  
- Optional: skip symbols with empty Breeze rows instead of failing the worker.  
- Merge **`sector` → `main`** when ready.  
- Re-run after **quota reset** or billing.

---

## Repo / remote

- Remote: `origin` (e.g. `github.com/arunurun/titan.git`).  
- Main feature commit: **`f37fb13`** (quota defaults + env flags + tests/CI + handoff). Doc-only tweaks may follow on `sector`; use `git log -3 --oneline`.
