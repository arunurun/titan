# Titan Control UI

Basic local UI for operational controls:

- Run Titan analysis on demand (`--live` or `--sector`).
- **Run Reconcile Now** — Supabase-only post-market report (manual trigger; use after daily Titan runs have stored analysis for ~1 week).
- Validate Breeze token from Supabase `session_config`.
- Paste a new `API_Session` token (or redirect URL), validate it, and persist to Supabase.
- Run portfolio summary from holdings PDF path or pasted holdings text fallback.

## Run

```bash
pip install -r requirements.txt
python control_ui/app.py
```

Open: `http://127.0.0.1:8787`

## Portfolio PDF / fallback holdings input

Use the **Portfolio Analysis (PDF + fallback text)** card in the control UI:

- Add a local PDF path (for example, broker holdings statement).
- Optionally paste holdings text as fallback (recommended).
- Supported fallback lines look like:
  - `NSE:RELIANCE, 10`
  - `INFY 5`
  - `BSE:TCS, 3`

The app tries PDF extraction first. If PDF parsing dependency is missing (`pypdf`/`PyPDF2`) or PDF text extraction fails, it falls back to pasted text and shows a limitation note in output.
