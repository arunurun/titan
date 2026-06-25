from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from breeze_connect import BreezeConnect
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_MAX_ANALYSIS_POSITIONS = 75
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from breeze_session_auth import parse_api_session_from_input, upsert_env_var  # noqa: E402
from config_loader import load_config  # noqa: E402
from portfolio_analysis import (  # noqa: E402
    analyze_portfolio_holdings,
    collect_holdings_input,
    portfolio_email_digest_plaintext,
)
from sector_registry import list_active_sector_ids  # noqa: E402
from breakout_scanner import run_breakout_scan  # noqa: E402

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
      button.btn-reconcile { background: var(--g-green); }
      button:disabled { opacity: 0.6; cursor: not-allowed; }
      .hidden { display: none; }
      details { margin-top: 10px; }
      details summary { cursor: pointer; color: var(--muted); font-size: 0.92rem; }
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

    {% if breeze_login_url %}
    <div class="card">
      <h3>Breeze login (session token)</h3>
      <p class="hint">Opens ICICI Breeze in a new tab. After login, copy <code>API_Session</code> from the redirect URL and paste it under <strong>Persist New Breeze Token</strong>.</p>
      <p><a href="{{ breeze_login_url }}" target="_blank" rel="noopener noreferrer">Open Breeze login</a></p>
    </div>
    {% endif %}

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
          <option value="live" {% if run_mode == "live" %}selected{% endif %}>Live (NIFTY)</option>
          <option value="sector" {% if run_mode == "sector" %}selected{% endif %}>Single sector</option>
          <option value="all_sectors" {% if run_mode == "all_sectors" %}selected{% endif %}>All sectors</option>
        </select>
        <label>Scope (single sector / all sectors)</label>
        <select name="titan_scope">
          <option value="priority" {% if titan_scope == "priority" %}selected{% endif %}>Top ranked only (Supabase priority list; refresh weekly Saturday)</option>
          <option value="full" {% if titan_scope == "full" %}selected{% endif %}>Full mapped universe</option>
        </select>
        <label>Sector ID (when mode = single sector)</label>
        <select name="sector_id">
          {% for sid in sectors %}
            <option value="{{ sid }}" {% if selected_sector == sid %}selected{% endif %}>{{ sid }}</option>
          {% endfor %}
        </select>
        <button type="submit">Run Analysis</button>
      </form>
    </div>

    <div class="card">
      <h3>Run Reconcile Now</h3>
      <p class="hint">Manual post-market reconcile. Run after daily Titan analysis has populated Supabase for several sessions (typically ~1 week). Sends one report-only email; no per-stock analysis.</p>
      <form method="post" action="/run-reconcile">
        <label>Reconcile scope</label>
        <select name="reconcile_scope">
          <option value="sector" {% if reconcile_scope == "sector" %}selected{% endif %}>Single sector (recommended)</option>
          <option value="all-stocks" {% if reconcile_scope == "all-stocks" %}selected{% endif %}>All stocks</option>
        </select>
        <label>Sector ID (when scope = sector)</label>
        <select name="reconcile_sector_id">
          {% for sid in sectors %}
            <option value="{{ sid }}" {% if reconcile_selected_sector == sid %}selected{% endif %}>{{ sid }}</option>
          {% endfor %}
        </select>
        <details>
          <summary>Advanced options</summary>
          <label>Backfill days (optional)</label>
          <input name="reconcile_backfill_days" value="{{ reconcile_backfill_days or '0' }}" />
          <label><input type="checkbox" name="reconcile_backfill_only" {% if reconcile_backfill_only %}checked{% endif %} /> Backfill only (skip report email)</label>
        </details>
        <button type="submit" class="btn-reconcile">Run Reconcile Now</button>
      </form>
      <p class="hint">Supabase-only: Breeze and live market fetch are blocked in reconcile mode.</p>
    </div>

    <div class="card">
      <h3>Find Breakouts</h3>
      <p class="hint">Scan Nifty Smallcap 100 and Microcap 250 for volume breakouts. Runs locally via <code>POST /api/breakouts</code> and emails the report when SMTP is configured (same env as sector digests).</p>
      <button type="button" id="findBreakoutsBtn">Find Breakouts</button>
      <pre id="breakoutResult" class="hidden" style="margin-top:12px"></pre>
    </div>

    <div class="card">
      <h3>Validate Breeze Token (from Supabase session_config)</h3>
      <form method="post" action="/validate-token">
        <button type="submit">Validate Token</button>
      </form>
      {% if token_status %}
        <p><strong>Status:</strong> <span class="{{ token_level }}">{{ token_status }}</span></p>
      {% endif %}
      {% if breeze_login_url %}
        <p><strong>Breeze login:</strong> <a href="{{ breeze_login_url }}" target="_blank" rel="noopener noreferrer">Open in new tab</a></p>
      {% endif %}
      {% if token_detail %}
        <pre>{{ token_detail }}</pre>
      {% endif %}
    </div>

    <div class="card">
      <h3>Persist New Breeze Token to Supabase</h3>
      {% if breeze_login_url %}
      <p class="hint"><a href="{{ breeze_login_url }}" target="_blank" rel="noopener noreferrer">Open Breeze login</a> to copy a fresh <code>API_Session</code>.</p>
      {% endif %}
      <p class="hint">Paste API_Session token OR full redirect URL containing <code>API_Session</code>. We validate before persisting.</p>
      <form method="post" action="/persist-token">
        <textarea name="token_input" rows="4" placeholder="Paste API_Session or redirect URL here..."></textarea>
        <label><input type="checkbox" name="also_write_env" checked /> Also update local <code>.env</code></label>
        <button type="submit">Validate + Persist Token</button>
      </form>
    </div>

    <div class="card">
      <h3>Portfolio Analysis (PDF + fallback text)</h3>
      <p class="hint">Provide PDF path for extraction. If PDF parser is unavailable or extraction fails, pasted holdings text is used.</p>
      <form method="post" action="/portfolio-analysis" enctype="multipart/form-data">
        <label>Upload portfolio PDF (preferred)</label>
        <input type="file" name="portfolio_pdf_file" accept=".pdf,application/pdf" />
        <label>Portfolio PDF path (optional)</label>
        <input name="portfolio_pdf_path" value="{{ portfolio_pdf_path or '' }}" placeholder="C:\\path\\to\\holdings.pdf" />
        <label>Fallback pasted holdings text (optional but recommended)</label>
        <textarea name="portfolio_holdings_text" rows="6" placeholder="NSE:RELIANCE, 10&#10;INFY 5&#10;BSE:TCS, 3">{{ portfolio_holdings_text or '' }}</textarea>
        <p class="hint">Analyzes up to {{ portfolio_max_analysis_positions }} holdings per run.</p>
        <button type="submit">Run Portfolio Summary</button>
      </form>
    </div>

    {% if run_output %}
      <div class="card">
        <h3>Last Analysis Run Output</h3>
        <pre>{{ run_output }}</pre>
      </div>
    {% endif %}
    </div>
    <script>
      (function () {
        const btn = document.getElementById("findBreakoutsBtn");
        const out = document.getElementById("breakoutResult");
        if (!btn || !out) return;

        function formatBreakout(data) {
          if (!data || typeof data !== "object") return "Empty breakout response.";
          const lines = [];
          lines.push("Breakout scan " + (data.ok ? "completed" : "failed"));
          if (data.scan_date) lines.push("Scan date: " + data.scan_date);
          if (data.duration_sec != null) lines.push("Duration: " + data.duration_sec + "s");
          if (data.tickers_scanned != null) lines.push("Tickers scanned: " + data.tickers_scanned);
          if (data.candidate_count != null) lines.push("Candidates: " + data.candidate_count);
          if (data.report_path) lines.push("Report: " + data.report_path);
          if (data.log_path) lines.push("Log: " + data.log_path);
          const candidates = Array.isArray(data.candidates) ? data.candidates : [];
          if (candidates.length) {
            lines.push("");
            lines.push("Candidates:");
            for (const c of candidates) {
              const ch = c.change_display || (c.change_pct != null ? c.change_pct + "%" : "?");
              const vol = c.volume_mult_display || (c.volume_mult != null ? c.volume_mult + "x" : "?");
              lines.push(
                "  " + c.ticker + " | " + (c.tier || "?") + " | " + ch +
                " | vol " + vol + " | RSI " + (c.rsi != null ? c.rsi : "?") +
                " | ADX " + (c.adx != null ? c.adx : "?")
              );
            }
          } else if (data.ok) {
            lines.push("");
            lines.push("No breakout candidates met filters today.");
          }
          return lines.join("\\n");
        }

        btn.addEventListener("click", async function () {
          btn.disabled = true;
          out.classList.remove("hidden");
          out.textContent = "Running breakout scan (POST /api/breakouts) ...";
          try {
            const res = await fetch("/api/breakouts", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ write_report: true, include_report_markdown: false }),
            });
            const data = await res.json().catch(function () { return {}; });
            if (!res.ok) {
              const msg = typeof data.error === "string" ? data.error : res.status + " " + res.statusText;
              out.textContent = "Find Breakouts failed:\\n" + msg;
              return;
            }
            let text = formatBreakout(data);
            text += "\\n\\nWhen SMTP is configured, the full report is also emailed (same inbox as sector digests).";
            out.textContent = text;
          } catch (err) {
            out.textContent = "Find Breakouts failed:\\n" + (err && err.message ? err.message : String(err));
          } finally {
            btn.disabled = false;
          }
        });
      })();
    </script>
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


def _safe_breeze_login_url() -> str:
    raw = (os.environ.get("BREEZE_API_KEY") or "").strip()
    if not raw:
        return ""
    return _breeze_login_url(raw)


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


def _run_titan_now(
    mode: str,
    sector_id: str,
    titan_scope: str,
) -> tuple[int, str]:
    mode = (mode or "sector").strip().lower()
    scope = (titan_scope or "priority").strip().lower()
    if scope not in ("full", "priority"):
        scope = "priority"
    cmd = [sys.executable, "main.py"]
    timeout_sec = 7200 if mode == "all_sectors" else 3600

    if mode == "live":
        cmd.append("--live")
    elif mode == "all_sectors":
        cmd.extend(["--all-sectors", "--exclude-sectors", "unknown,non_equity"])
        if scope == "priority":
            cmd.extend(["--sector-priority-only", "--sector-priority-top-n", "10"])
    else:
        sid = (sector_id or "").strip() or "defence"
        cmd.extend(["--sector", sid, "--sector-digest"])
        if scope == "priority":
            cmd.extend(["--sector-priority-only", "--sector-priority-top-n", "10"])

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        env=dict(os.environ),
    )
    output = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _run_reconcile_now(
    scope: str,
    sector_id: str,
    backfill_days: str,
    backfill_only: bool,
) -> tuple[int, str]:
    scope_norm = (scope or "all-stocks").strip().lower()
    if scope_norm not in ("all-stocks", "sector"):
        scope_norm = "all-stocks"
    cmd = [sys.executable, "scripts/run_post_market_reconcile.py", "--scope", scope_norm]
    if scope_norm == "sector":
        sid = (sector_id or "").strip() or "defence"
        cmd.extend(["--sector", sid])
    backfill_days_raw = (backfill_days or "").strip()
    if backfill_days_raw:
        cmd.extend(["--backfill-days", backfill_days_raw])
    if backfill_only:
        cmd.append("--backfill-only")
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=1800,
        env=dict(os.environ),
    )
    output = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _sector_choices() -> list[str]:
    try:
        sectors = [
            s
            for s in list_active_sector_ids(include_unknown=False)
            if s not in {"unknown", "non_equity"}
        ]
        if sectors:
            return sectors
    except Exception:
        pass
    return [
        "ai",
        "auto",
        "auto_ancillary",
        "banks_private",
        "capital_goods_industrials",
        "cement_building_materials",
        "chemicals",
        "consumer_discretionary",
        "defence",
        "fmcg_staples",
        "infrastructure_construction",
        "insurance",
        "it",
        "logistics",
        "media",
        "metals_mining",
        "nbfc_financial_services",
        "oil_gas_energy",
        "pharma_healthcare",
        "power_utilities",
        "realty_reits",
        "telecom",
        "textiles",
    ]


def _render_page(**kwargs):
    kwargs = dict(kwargs)
    sectors = _sector_choices()
    selected_sector = str(kwargs.pop("selected_sector", None) or "defence")
    if selected_sector not in sectors:
        selected_sector = sectors[0] if sectors else "defence"
    run_mode = str(kwargs.pop("run_mode", None) or "sector")
    reconcile_scope = str(kwargs.pop("reconcile_scope", None) or "sector")
    if reconcile_scope not in ("all-stocks", "sector"):
        reconcile_scope = "all-stocks"
    reconcile_selected_sector = str(kwargs.pop("reconcile_selected_sector", None) or "defence")
    if reconcile_selected_sector not in sectors:
        reconcile_selected_sector = sectors[0] if sectors else "defence"
    reconcile_backfill_days = str(kwargs.pop("reconcile_backfill_days", None) or "0")
    reconcile_backfill_only = bool(kwargs.pop("reconcile_backfill_only", None) or False)
    portfolio_pdf_path = str(kwargs.pop("portfolio_pdf_path", None) or "")
    portfolio_holdings_text = str(kwargs.pop("portfolio_holdings_text", None) or "")
    titan_scope = str(kwargs.pop("titan_scope", None) or "priority")
    if titan_scope not in ("full", "priority"):
        titan_scope = "priority"
    breeze_login_url = str(kwargs.pop("breeze_login_url", None) or "")
    return render_template_string(
        TEMPLATE,
        sectors=sectors,
        selected_sector=selected_sector,
        run_mode=run_mode,
        reconcile_scope=reconcile_scope,
        reconcile_selected_sector=reconcile_selected_sector,
        reconcile_backfill_days=reconcile_backfill_days,
        reconcile_backfill_only=reconcile_backfill_only,
        titan_scope=titan_scope,
        breeze_login_url=breeze_login_url,
        portfolio_pdf_path=portfolio_pdf_path,
        portfolio_holdings_text=portfolio_holdings_text,
        portfolio_max_analysis_positions=PORTFOLIO_MAX_ANALYSIS_POSITIONS,
        **kwargs,
    )


@app.get("/")
def index():
    load_dotenv(ROOT / ".env", override=False)
    return _render_page(breeze_login_url=_safe_breeze_login_url())


@app.post("/run-analysis")
def run_analysis():
    load_dotenv(ROOT / ".env", override=False)
    mode = request.form.get("mode", "sector").strip()
    sector_id = request.form.get("sector_id", "defence")
    titan_scope = request.form.get("titan_scope", "priority").strip()
    try:
        code, output = _run_titan_now(
            mode,
            sector_id,
            titan_scope,
        )
        level = "ok" if code == 0 else "err"
        msg = f"Analysis finished with exit code {code}."
        return _render_page(
            message=msg,
            level=level,
            run_output=output,
            run_mode=mode,
            selected_sector=sector_id,
            titan_scope=titan_scope,
            breeze_login_url=_safe_breeze_login_url(),
        )
    except Exception as exc:  # noqa: BLE001
        return _render_page(
            message=f"Run failed: {exc}",
            level="err",
            run_mode=mode,
            selected_sector=sector_id,
            titan_scope=titan_scope,
            breeze_login_url=_safe_breeze_login_url(),
        )


@app.post("/run-reconcile")
def run_reconcile():
    load_dotenv(ROOT / ".env", override=False)
    scope = request.form.get("reconcile_scope", "sector").strip()
    sector_id = request.form.get("reconcile_sector_id", "defence").strip()
    backfill_days = request.form.get("reconcile_backfill_days", "0").strip()
    backfill_only = request.form.get("reconcile_backfill_only") == "on"
    try:
        code, output = _run_reconcile_now(scope, sector_id, backfill_days, backfill_only)
        level = "ok" if code == 0 else "err"
        msg = f"Reconcile finished with exit code {code}."
        return _render_page(
            message=msg,
            level=level,
            run_output=output,
            reconcile_scope=scope,
            reconcile_selected_sector=sector_id,
            reconcile_backfill_days=backfill_days or "0",
            reconcile_backfill_only=backfill_only,
            breeze_login_url=_safe_breeze_login_url(),
        )
    except Exception as exc:  # noqa: BLE001
        return _render_page(
            message=f"Reconcile failed: {exc}",
            level="err",
            reconcile_scope=scope,
            reconcile_selected_sector=sector_id,
            reconcile_backfill_days=backfill_days or "0",
            reconcile_backfill_only=backfill_only,
            breeze_login_url=_safe_breeze_login_url(),
        )


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
        return _render_page(
            token_status=status,
            token_level=token_level,
            token_detail=detail,
            breeze_login_url=_breeze_login_url(api_key),
            message="Token validation complete.",
            level="ok" if ok else "warn",
        )
    except Exception as exc:  # noqa: BLE001
        return _render_page(message=f"Token validation failed: {exc}", level="err")


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
            return _render_page(
                message=f"Provided token is invalid: {detail}",
                level="err",
                breeze_login_url=_breeze_login_url(api_key),
            )
        _persist_token_to_supabase(token)
        if also_write_env:
            upsert_env_var(ROOT / ".env", "BREEZE_SESSION_TOKEN", token)
        return _render_page(
            message="Token validated and persisted to Supabase session_config.",
            level="ok",
            token_status="VALID",
            token_level="ok",
            token_detail="New token stored successfully.",
            breeze_login_url=_breeze_login_url(api_key),
        )
    except Exception as exc:  # noqa: BLE001
        return _render_page(message=f"Persist token failed: {exc}", level="err")


@app.post("/portfolio-analysis")
def run_portfolio_analysis():
    load_dotenv(ROOT / ".env", override=False)
    pdf_path = (request.form.get("portfolio_pdf_path") or "").strip()
    uploaded = request.files.get("portfolio_pdf_file")
    holdings_text = request.form.get("portfolio_holdings_text", "")
    max_positions = PORTFOLIO_MAX_ANALYSIS_POSITIONS
    tmp_pdf_path: str | None = None
    try:
        if uploaded and uploaded.filename:
            if not uploaded.filename.lower().endswith(".pdf"):
                return _render_page(
                    message="Uploaded file must be a PDF.",
                    level="err",
                    portfolio_pdf_path=pdf_path,
                    portfolio_holdings_text=holdings_text,
                )
            with tempfile.NamedTemporaryFile(prefix="titan_portfolio_", suffix=".pdf", delete=False) as tmp:
                uploaded.save(tmp.name)
                tmp_pdf_path = tmp.name
                pdf_path = tmp_pdf_path

        holdings, source, limitations = collect_holdings_input(
            pdf_path=pdf_path,
            pasted_holdings_text=holdings_text,
        )
        if not holdings:
            return _render_page(
                message="No holdings could be parsed. Review format and retry with fallback text.",
                level="warn",
                run_output=portfolio_email_digest_plaintext(
                    source=source,
                    limitations=limitations,
                    parsed_count=0,
                    result={"summary": {"requested_positions": 0}, "rows": []},
                    gemini_keys=load_config().gemini_api_keys,
                ),
                portfolio_pdf_path=pdf_path,
                portfolio_holdings_text=holdings_text,
            )
        result = analyze_portfolio_holdings(holdings, max_positions=max_positions)
        level = "ok" if result.get("summary", {}).get("analyzed_positions", 0) > 0 else "warn"
        _cfg = load_config()
        return _render_page(
            message=f"Portfolio analysis completed from {source} input.",
            level=level,
            run_output=portfolio_email_digest_plaintext(
                source=source,
                limitations=limitations,
                parsed_count=len(holdings),
                result=result,
                gemini_keys=_cfg.gemini_api_keys,
            ),
            portfolio_pdf_path=pdf_path,
            portfolio_holdings_text=holdings_text,
        )
    except Exception as exc:  # noqa: BLE001
        return _render_page(
            message=f"Portfolio analysis failed: {exc}",
            level="err",
            portfolio_pdf_path=pdf_path,
            portfolio_holdings_text=holdings_text,
        )
    finally:
        if tmp_pdf_path:
            try:
                os.remove(tmp_pdf_path)
            except OSError:
                pass


def _parse_breakout_request() -> tuple[bool, bool]:
    """Return (write_report, include_report_markdown) from JSON or form body."""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        write_report = bool(payload.get("write_report", True))
        include_report = bool(payload.get("include_report_markdown", False))
        return write_report, include_report
    write_report = request.form.get("write_report", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    include_report = request.form.get("include_report_markdown", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    return write_report, include_report


@app.post("/api/breakouts")
def find_breakouts():
    """Run the small/micro-cap breakout scanner and return JSON results."""
    load_dotenv(ROOT / ".env", override=False)
    write_report, include_report = _parse_breakout_request()
    try:
        result = run_breakout_scan(
            output_dir=ROOT / "data" / "reports" / "breakout",
            write_report=write_report,
            emit_to_stdout=False,
        )
        if not include_report:
            result.pop("report_markdown", None)
        status = 200 if result.get("ok") else 500
        return jsonify(result), status
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=False)
    host = os.environ.get("TITAN_UI_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_raw = os.environ.get("TITAN_UI_PORT", "8787").strip() or "8787"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8787
    app.run(host=host, port=port, debug=False)
