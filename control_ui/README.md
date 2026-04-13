# Titan Control UI

Basic local UI for operational controls:

- Run Titan analysis on demand (`--live` or `--sector`).
- Validate Breeze token from Supabase `session_config`.
- Paste a new `API_Session` token (or redirect URL), validate it, and persist to Supabase.

## Run

```bash
pip install -r requirements.txt
python control_ui/app.py
```

Open: `http://127.0.0.1:8787`
