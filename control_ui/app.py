from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from flask import Flask, render_template_string, request
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breeze_session_auth import parse_api_session_from_input, upsert_env_var  # noqa: E402

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Titan Control Panel</title>
    <style>
      :root {
        --g-blue: #4285f4;
        --g-red: #ea4335;
        --g-yellow: #fbbc05;
        --g-green: #34a853;
        --text: #202124;
        --muted: #5f6368;
        --border: #e0e3e7;
      }
      * { box-sizing: border-box; }
      body {
        font-family: Arial, sans-serif;
        margin: 0;
        padding: 12px;
        color: var(--text);
        background: #fff;
      }
      .container {
        max-width: 720px;
        margin: 0 auto;
      }
      h1 {
        margin: 0 0 10px;
        font-size: 1.35rem;
      }
      .logo-line {
        height: 4px;
        border-radius: 999px;
        margin: 0 0 14px;
        background: linear-gradient(90deg, var(--g-blue) 0 25%, var(--g-red) 25% 50%, var(--g-yellow) 50% 75%, var(--g-green) 75% 100%);
      }
      .card {
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 14px;
        margin-bottom: 12px;
        background: #fff;
      }
      h3 {
        margin: 0 0 10px;
        font-size: 1.05rem;
      }
      label {
        display: block;
        margin-top: 8px;
        font-size: 0.92rem;
      }
      textarea, input, select {
        width: 100%;
        padding: 10px;
        margin-top: 6px;
        border: 1px solid var(--border);
        border-radius: 8px;
        font-size: 0.95rem;
      }
      button {
        width: 100%;
        margin-top: 12px;
        padding: 11px 14px;
        border: none;
        border-radius: 999px;
        cursor: pointer;
        color: #fff;
        font-weight: bold;
        font-size: 0.95rem;
        background: var(--g-blue);
      }
      button:hover { filter: brightness(0.95); }
      .ok { color: var(--g-green); font-weight: bold; }
      .warn { color: #c26401; font-weight: bold; }
      .err { color: var(--g-red); font-weight: bold; }
      pre {
        background: #f8f9fa;
        border: 1px solid var(--border);
        padding: 10px;
        overflow: auto;
        max-height: 320px;
        border-radius: 8px;
      }
      code {
        background: #f1f3f4;
        padding: 2px 4px;
        border-radius: 4px;
      }
      .hint { color: var(--muted); font-size: 0.9rem; }
      .chip {
        display: inline-block;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 0.8rem;
        color: #fff;
        background: var(--g-green);
      }
      @media (min-width: 760px) {
        body { padding: 18px; }
        h1 { font-size: 1.6rem; }
      }
    </style>
  </head>
  <body>
    <div class="container">
    <h1>Titan Control Panel <span class="chip">Mobile</span></h1>
    <div class="logo-line"></div>
    <p class="hint">Local-only utility page to trigger analysis and manage Breeze token.</p>

    {% if message %}
      <div class="card">
        <div class="{{ level }}">{{ message }}</div>
      </div>
    {% endif %}

    <div class="card">
      <h3>Run Titan Analysis Now</h3>
      <form method="post" action="/run-analysis">
        <label>Mode</label>
        <select name="mode">
          <option value="live">Live (NIFTY)</option>
          <option value="sector" selected>Sector (Digest)</option>
        </select>
        <label>Sector ID (for sector mode)</label>
        <input name="sector_id" value="defence" />
        <label>Max symbols (optional; leave blank for full sector list)</label>
        <input name="max_symbols" value="" placeholder="all" />
        <label>Workers (optional)</label>
        <input name="workers" value="2" />
        <button type="submit">Run Analysis</button>
      </form>
    </div>

    <div class="card">
      <h3>Validate Breeze Token (from Supabase session_config)</h3>
      <form method="post" action="/validate-token">
        <button type="submit">Validate Token</button>
      </form>
      {% if token_status %}
        <p><strong>Status:</strong> <span class="{{ token_level }}">{{ token_status }}</span></p>
      {% endif %}
      {% if login_url %}
        <p><strong>Breeze Login URL:</strong> <a href="{{ login_url }}" target="_blank">{{ login_url }}</a></p>
      {% endif %}
      {% if token_detail %}
        <pre>{{ token_detail }}</pre>
      {% endif %}
    </div>

    <div class="card">
      <h3>Persist New Breeze Token to Supabase</h3>
      <p class="hint">Paste API_Session token OR full redirect URL containing <code>API_Session</code>. We validate before persisting.</p>
      <form method="post" action="/persist-token">
        <textarea name="token_input" rows="4" placeholder="Paste API_Session or redirect URL here..."></textarea>
        <label><input type="checkbox" name="also_write_env" checked /> Also update local <code>.env</code></label>
        <button type="submit">Validate + Persist Token</button>
      </form>
    </div>

    {% if run_output %}
      <div class="card">
        <h3>Last Analysis Run Output</h3>
        <pre>{{ run_output }}</pre>
      </div>
    {% endif %}
    </div>
  </body>
</html>
"""


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _supabase_client():
    return create_client(_required("SUPABASE_URL"), _required("SUPABASE_KEY"))


def _breeze_login_url(api_key: str) -> str:
    return f"https://api.icicidirect.com/apiuser/login?api_key={quote(api_key, safe='')}"


def _load_token_from_supabase() -> str:
    client = _supabase_client()
    res = client.table("session_config").select("breeze_session_token").eq("id", 1).limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        raise RuntimeError("No row found in session_config with id=1")
    token = (data[0].get("breeze_session_token") or "").strip()
    if not token:
        raise RuntimeError("breeze_session_token is empty in session_config")
    return token


def _validate_breeze_token(api_key: str, api_secret: str, token: str) -> tuple[bool, str]:
    breeze = BreezeConnect(api_key=api_key)
    try:
        breeze.generate_session(api_secret=api_secret, session_token=token)
        return True, "Token is valid."
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _persist_token_to_supabase(token: str) -> None:
    client = _supabase_client()
    client.table("session_config").upsert(
        {"id": 1, "breeze_session_token": token},
        on_conflict="id",
    ).execute()


def _run_titan_now(mode: str, sector_id: str, max_symbols: str, workers: str) -> tuple[int, str]:
    cmd = [sys.executable, "main.py"]
    if mode == "live":
        cmd.append("--live")
    else:
        sid = (sector_id or "").strip() or "defence"
        cmd.extend(["--sector", sid, "--sector-digest"])
        if (max_symbols or "").strip():
            cmd.extend(["--sector-max-symbols", max_symbols.strip()])
        if (workers or "").strip():
            cmd.extend(["--sector-workers", workers.strip()])

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=600,
        env=dict(os.environ),
    )
    output = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, output.strip()


@app.get("/")
def index():
    return render_template_string(TEMPLATE)


@app.post("/run-analysis")
def run_analysis():
    load_dotenv(ROOT / ".env", override=False)
    mode = request.form.get("mode", "sector").strip()
    sector_id = request.form.get("sector_id", "defence")
    max_symbols = request.form.get("max_symbols", "")
    workers = request.form.get("workers", "")
    try:
        code, output = _run_titan_now(mode, sector_id, max_symbols, workers)
        level = "ok" if code == 0 else "err"
        msg = f"Analysis finished with exit code {code}."
        return render_template_string(TEMPLATE, message=msg, level=level, run_output=output)
    except Exception as exc:  # noqa: BLE001
        return render_template_string(TEMPLATE, message=f"Run failed: {exc}", level="err")


@app.post("/validate-token")
def validate_token():
    load_dotenv(ROOT / ".env", override=False)
    try:
        api_key = _required("BREEZE_API_KEY")
        api_secret = _required("BREEZE_SECRET")
        token = _load_token_from_supabase()
        ok, detail = _validate_breeze_token(api_key, api_secret, token)
        status = "VALID" if ok else "INVALID"
        token_level = "ok" if ok else "err"
        return render_template_string(
            TEMPLATE,
            token_status=status,
            token_level=token_level,
            token_detail=detail,
            login_url=_breeze_login_url(api_key),
            message="Token validation complete.",
            level="ok" if ok else "warn",
        )
    except Exception as exc:  # noqa: BLE001
        return render_template_string(TEMPLATE, message=f"Token validation failed: {exc}", level="err")


@app.post("/persist-token")
def persist_token():
    load_dotenv(ROOT / ".env", override=False)
    raw = (request.form.get("token_input") or "").strip()
    also_write_env = request.form.get("also_write_env") == "on"
    try:
        api_key = _required("BREEZE_API_KEY")
        api_secret = _required("BREEZE_SECRET")
        token = parse_api_session_from_input(raw)
        ok, detail = _validate_breeze_token(api_key, api_secret, token)
        if not ok:
            return render_template_string(
                TEMPLATE,
                message=f"Provided token is invalid: {detail}",
                level="err",
                login_url=_breeze_login_url(api_key),
            )
        _persist_token_to_supabase(token)
        if also_write_env:
            upsert_env_var(ROOT / ".env", "BREEZE_SESSION_TOKEN", token)
        return render_template_string(
            TEMPLATE,
            message="Token validated and persisted to Supabase session_config.",
            level="ok",
            token_status="VALID",
            token_level="ok",
            token_detail="New token stored successfully.",
            login_url=_breeze_login_url(api_key),
        )
    except Exception as exc:  # noqa: BLE001
        return render_template_string(TEMPLATE, message=f"Persist token failed: {exc}", level="err")


if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=False)
    host = os.environ.get("TITAN_UI_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_raw = os.environ.get("TITAN_UI_PORT", "8787").strip() or "8787"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8787
    app.run(host=host, port=port, debug=False)
