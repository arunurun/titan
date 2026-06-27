"""Refactored thin UI that calls internal API endpoints instead of directly executing logic."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from api.app import create_app as create_api_app
from portfolio_analysis import (
    analyze_portfolio_holdings,
    collect_holdings_input,
    portfolio_email_digest_plaintext,
)
from config_loader import load_config
from sector_registry import list_active_sector_ids

PORTFOLIO_MAX_ANALYSIS_POSITIONS = 75

app = Flask(__name__)

# Mount the internal API app on a sub-path (or integrate blueprints directly)
api_app = create_api_app()
for blueprint in api_app.blueprints.values():
    app.register_blueprint(blueprint)

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
      button:disabled { opacity: 0.6; cursor: not-allowed; }
      .hidden { display: none; }
      .ok { color: var(--g-green); font-weight: bold; }
      .err { color: var(--g-red); font-weight: bold; }
      pre {
        background: #f8f9fa;
        border: 1px solid var(--border);
        padding: 10px;
        overflow: auto;
        max-height: 320px;
        border-radius: 8px;
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
    <h1>Titan Control Panel <span class="chip">Refactored</span></h1>
    <div class="logo-line"></div>
    <p class="hint">API-driven operational controls.</p>

    {% if breeze_login_url %}
    <div class="card">
      <h3>Breeze login (session token)</h3>
      <p class="hint">Opens ICICI Breeze in a new tab. After login, copy <code>API_Session</code> from the redirect URL and paste it below.</p>
      <p><a href="{{ breeze_login_url }}" target="_blank" rel="noopener noreferrer">Open Breeze login</a></p>
    </div>
    {% endif %}

    {% if message %}
      <div class="card">
        <div class="{{ level }}">{{ message }}</div>
      </div>
    {% endif %}

    <div class="card">
      <h3>Persist New Breeze Token to Supabase</h3>
      {% if breeze_login_url %}
      <p class="hint"><a href="{{ breeze_login_url }}" target="_blank" rel="noopener noreferrer">Open Breeze login</a> to copy a fresh <code>API_Session</code>.</p>
      {% endif %}
      <p class="hint">Paste API_Session token OR full redirect URL containing <code>API_Session</code>. We validate before persisting.</p>
      <form id="persistForm">
        <textarea id="tokenInput" rows="4" placeholder="Paste API_Session or redirect URL here..."></textarea>
        <label><input type="checkbox" id="alsoWriteEnv" /> Also update local <code>.env</code></label>
        <button type="button" id="persistBtn">Validate + Persist Token</button>
      </form>
      <pre id="persistResult" class="hidden"></pre>
    </div>

    <div class="card">
      <h3>Validate Breeze Token (from Supabase session_config)</h3>
      <button type="button" id="validateBtn">Validate Token</button>
      <pre id="validateResult" class="hidden"></pre>
    </div>

    {% if run_output %}
      <div class="card">
        <h3>Last Run Output</h3>
        <pre>{{ run_output }}</pre>
      </div>
    {% endif %}
    </div>

    <script>
      (function () {
        const persistBtn = document.getElementById("persistBtn");
        const persistForm = document.getElementById("persistForm");
        const persistResult = document.getElementById("persistResult");
        const validateBtn = document.getElementById("validateBtn");
        const validateResult = document.getElementById("validateResult");

        if (persistBtn) {
          persistBtn.addEventListener("click", async function () {
            persistBtn.disabled = true;
            persistResult.classList.remove("hidden");
            persistResult.textContent = "Persisting token...";
            try {
              const token = document.getElementById("tokenInput").value || "";
              const alsoWrite = document.getElementById("alsoWriteEnv").checked;
              const res = await fetch("/api/token/persist", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token_input: token, also_write_env: alsoWrite }),
              });
              const data = await res.json().catch(() => ({}));
              persistResult.textContent = (data.message || data.error || "Response: " + res.status) + "\n" + (data.ok ? "✓ Success" : "✗ Failed");
              persistResult.className = data.ok ? "ok" : "err";
            } catch (err) {
              persistResult.textContent = "Error: " + (err.message || String(err));
              persistResult.className = "err";
            } finally {
              persistBtn.disabled = false;
            }
          });
        }

        if (validateBtn) {
          validateBtn.addEventListener("click", async function () {
            validateBtn.disabled = true;
            validateResult.classList.remove("hidden");
            validateResult.textContent = "Validating token...";
            try {
              const res = await fetch("/api/token/validate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
              });
              const data = await res.json().catch(() => ({}));
              const status = data.status || (data.ok ? "OK" : "ERROR");
              validateResult.textContent = "Status: " + status + "\nDetail: " + (data.detail || data.error || "");
              validateResult.className = data.ok ? "ok" : "err";
            } catch (err) {
              validateResult.textContent = "Error: " + (err.message || String(err));
              validateResult.className = "err";
            } finally {
              validateBtn.disabled = false;
            }
          });
        }
      })();
    </script>
  </body>
</html>
"""


def _breeze_login_url(api_key: str) -> str:
    return f"https://api.icicidirect.com/apiuser/login?api_key={quote(api_key, safe='')}"


def _safe_breeze_login_url() -> str:
    raw = (os.environ.get("BREEZE_API_KEY") or "").strip()
    if not raw:
        return ""
    return _breeze_login_url(raw)


@app.get("/")
def index():
    load_dotenv(ROOT / ".env", override=False)
    return render_template_string(
        TEMPLATE,
        breeze_login_url=_safe_breeze_login_url(),
        message="",
        level="ok",
    )


if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=False)
    host = os.environ.get("TITAN_UI_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_raw = os.environ.get("TITAN_UI_PORT", "8787").strip() or "8787"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8787
    app.run(host=host, port=port, debug=False)
