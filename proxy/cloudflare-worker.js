/**
 * Tokenless dispatch proxy for Titan Mobile Control UI.
 *
 * Deploy as Cloudflare Worker and set secrets:
 * - GITHUB_PAT
 * - REPO_OWNER (e.g., arunurun)
 * - REPO_NAME (e.g., titan)
 * - BREEZE_API_KEY (optional: enables GET /breeze-login redirect to ICICI login)
 * - SUPABASE_URL (optional: enables GET /insights/latest for TWA / mobile UI)
 * - SUPABASE_SERVICE_ROLE_KEY (optional: same; service role — never expose to browser)
 * - GET /insights/github-run/:id — digest for a specific GitHub Actions run (+ sector)
 */

const ALLOWED_WORKFLOWS = new Set([
  "run_titan_now.yml",
  "validate_breeze_token_manual.yml",
  "persist_breeze_token_manual.yml",
  "refresh_sector_rankings_weekly.yml",
]);
const RUN_MODES = new Set(["sector", "all_sectors", "live", "custom", "portfolio"]);
const TITAN_SCOPES = new Set(["full", "priority"]);
const ALLOWED_EXCHANGES = new Set(["NSE", "BSE"]);
const SYMBOL_RE = /^[A-Z0-9&._-]{1,25}$/;
const SECTOR_ID_RE = /^[a-z0-9_]{1,64}$/;

function toStringInput(v) {
  return typeof v === "string" ? v.trim() : "";
}

function parseBoundedInt(raw, field, { min = 1, max = 500 } = {}) {
  const val = toStringInput(raw);
  if (!val) return "";
  if (!/^\d+$/.test(val)) {
    throw new Error(`${field} must be a whole number.`);
  }
  const n = Number(val);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new Error(`${field} must be between ${min} and ${max}.`);
  }
  return String(n);
}

function sanitizeCustomSymbols(raw) {
  const tokens = toStringInput(raw)
    .toUpperCase()
    .split(/[\s,;\n\r\t]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (!tokens.length) {
    throw new Error("custom_symbols is required for mode=custom");
  }
  const out = [];
  const seen = new Set();
  for (const sym of tokens) {
    if (!SYMBOL_RE.test(sym)) {
      throw new Error(`Invalid custom symbol: ${sym}`);
    }
    if (!seen.has(sym)) {
      seen.add(sym);
      out.push(sym);
    }
  }
  if (out.length > 120) {
    throw new Error("custom_symbols exceeds max size (120)");
  }
  return out.join(",");
}

function sanitizePortfolioHoldingsJson(raw) {
  const txt = toStringInput(raw);
  if (!txt) {
    throw new Error("portfolio_holdings_json is required for mode=portfolio");
  }
  if (txt.length > 250000) {
    throw new Error("portfolio_holdings_json is too long");
  }
  let payload;
  try {
    payload = JSON.parse(txt);
  } catch (_e) {
    throw new Error("portfolio_holdings_json must be valid JSON");
  }
  if (!Array.isArray(payload)) {
    throw new Error("portfolio_holdings_json must be an array");
  }
  if (payload.length < 1 || payload.length > 300) {
    throw new Error("portfolio_holdings_json must contain 1..300 holdings");
  }
  const out = [];
  for (const row of payload) {
    if (!row || typeof row !== "object") {
      throw new Error("each portfolio holding must be an object");
    }
    const symbol = toStringInput(row.symbol).toUpperCase();
    if (!SYMBOL_RE.test(symbol)) {
      throw new Error(`Invalid portfolio symbol: ${symbol || "<empty>"}`);
    }
    const quantityRaw = row.quantity ?? row.qty;
    const quantity = Number(quantityRaw);
    if (!Number.isFinite(quantity) || quantity === 0) {
      throw new Error(`Invalid quantity for symbol ${symbol}`);
    }
    const clean = {
      symbol,
      quantity: quantity,
    };
    const ex = toStringInput(row.exchange).toUpperCase();
    if (ex) {
      if (!ALLOWED_EXCHANGES.has(ex)) {
        throw new Error(`Invalid exchange for symbol ${symbol}`);
      }
      clean.exchange = ex;
    }
    const avgBuyRaw = row.avg_buy_price ?? row.buy_price ?? row.avg_price;
    if (avgBuyRaw !== undefined && avgBuyRaw !== null && toStringInput(String(avgBuyRaw)) !== "") {
      const avgBuy = Number(avgBuyRaw);
      if (!Number.isFinite(avgBuy) || avgBuy <= 0) {
        throw new Error(`Invalid avg_buy_price for symbol ${symbol}`);
      }
      clean.avg_buy_price = avgBuy;
    }
    out.push(clean);
  }
  return JSON.stringify(out);
}

function sanitizeRunTitanInputs(inputObj) {
  const input = inputObj && typeof inputObj === "object" ? inputObj : {};
  const mode = toStringInput(input.mode) || "sector";
  if (!RUN_MODES.has(mode)) {
    throw new Error("mode is invalid");
  }

  const sectorId = toStringInput(input.sector_id).toLowerCase();
  const workers = parseBoundedInt(input.workers, "workers", { min: 1, max: 16 });
  const maxSymbols = parseBoundedInt(input.max_symbols, "max_symbols", { min: 1, max: 500 });
  const allSectorWorkers = parseBoundedInt(input.all_sector_workers, "all_sector_workers", {
    min: 1,
    max: 200,
  });
  const portfolioMaxPositions = parseBoundedInt(
    input.portfolio_max_positions,
    "portfolio_max_positions",
    { min: 1, max: 300 },
  );

  const cleaned = {
    mode,
    sector_id: "",
    max_symbols: maxSymbols,
    workers,
    all_sector_workers: allSectorWorkers,
    portfolio_holdings_json: "",
    portfolio_max_positions: portfolioMaxPositions,
    titan_scope: "",
    priority_top_n: "",
  };

  if (mode === "sector") {
    if (!sectorId || !SECTOR_ID_RE.test(sectorId)) {
      throw new Error("sector_id is required and must match [a-z0-9_]{1,64} for mode=sector");
    }
    cleaned.sector_id = sectorId;
  } else if (mode === "custom") {
    const effectiveSectorId = sectorId || "custom_ui";
    if (!SECTOR_ID_RE.test(effectiveSectorId)) {
      throw new Error("sector_id for custom mode must match [a-z0-9_]{1,64}");
    }
    const customExchange = toStringInput(input.custom_exchange).toUpperCase() || "NSE";
    if (!ALLOWED_EXCHANGES.has(customExchange)) {
      throw new Error("custom_exchange must be NSE or BSE");
    }
    cleaned.sector_id = effectiveSectorId;
    cleaned.custom_symbols = sanitizeCustomSymbols(input.custom_symbols);
    cleaned.custom_exchange = customExchange;
  } else if (mode === "portfolio") {
    const customExchange = toStringInput(input.custom_exchange).toUpperCase() || "NSE";
    if (!ALLOWED_EXCHANGES.has(customExchange)) {
      throw new Error("custom_exchange must be NSE or BSE");
    }
    cleaned.custom_exchange = customExchange;
    cleaned.portfolio_holdings_json = sanitizePortfolioHoldingsJson(input.portfolio_holdings_json);
    if (!cleaned.portfolio_max_positions) {
      cleaned.portfolio_max_positions = "75";
    }
    cleaned.sector_id = "";
  } else {
    cleaned.sector_id = "";
  }

  if (mode === "sector" || mode === "all_sectors") {
    const ts = toStringInput(input.titan_scope).toLowerCase() || "priority";
    if (!TITAN_SCOPES.has(ts)) {
      throw new Error("titan_scope must be full or priority");
    }
    cleaned.titan_scope = ts;
    const ptn = toStringInput(input.priority_top_n);
    if (ptn) {
      if (!/^\d+$/.test(ptn)) {
        throw new Error("priority_top_n must be numeric");
      }
      const n = Number(ptn);
      if (n < 1 || n > 25) {
        throw new Error("priority_top_n out of range");
      }
    }
    cleaned.priority_top_n = ptn;
  }

  return cleaned;
}

function sanitizeRefreshRankingsInputs(inputObj) {
  const input = inputObj && typeof inputObj === "object" ? inputObj : {};
  const top_n = parseBoundedInt(input.top_n, "top_n", { min: 1, max: 25 });
  return { top_n: top_n || "10" };
}

function sanitizeDispatchPayload(workflow, inputs) {
  if (workflow === "run_titan_now.yml") {
    return sanitizeRunTitanInputs(inputs);
  }
  if (workflow === "refresh_sector_rankings_weekly.yml") {
    return sanitizeRefreshRankingsInputs(inputs);
  }
  if (workflow === "persist_breeze_token_manual.yml") {
    const breezeTokenInput = toStringInput(inputs?.breeze_token_input);
    if (!breezeTokenInput) {
      throw new Error("breeze_token_input is required");
    }
    if (breezeTokenInput.length > 5000) {
      throw new Error("breeze_token_input is too long");
    }
    return { breeze_token_input: breezeTokenInput };
  }
  return {};
}

function json(body, status = 200) {
  const noBody = status === 204 || status === 205 || status === 304;
  return new Response(noBody ? null : JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "content-type",
    },
  });
}

async function gh(env, path, method = "GET", body = null) {
  if (!env.GITHUB_PAT) {
    throw new Error("Missing worker secret: GITHUB_PAT");
  }
  if (!env.REPO_OWNER || !env.REPO_NAME) {
    throw new Error("Missing worker vars: REPO_OWNER and/or REPO_NAME");
  }
  const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}${path}`;
  const res = await fetch(url, {
    method,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_PAT}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "titan-mobile-control-proxy",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const txt = await res.text();
  if (!res.ok) {
    if (res.status === 404) {
      throw new Error(
        `404 from GitHub API for ${env.REPO_OWNER}/${env.REPO_NAME}. ` +
          `Check REPO_OWNER/REPO_NAME and PAT repo access. Raw: ${txt}`,
      );
    }
    if (res.status === 401 || res.status === 403) {
      throw new Error(`Auth/permission error from GitHub (${res.status}). Raw: ${txt}`);
    }
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }
  if (!txt) return null;
  return JSON.parse(txt);
}

function normalizeWorkflowInputs(raw) {
  if (!raw || typeof raw !== "object") return {};
  const out = {};
  for (const [k, v] of Object.entries(raw)) {
    if (v == null) continue;
    const s = typeof v === "string" ? v.trim() : String(v).trim();
    if (s) out[k] = s;
  }
  return out;
}

function uniqueSectorsFromDigestRows(rows) {
  const seen = new Set();
  const out = [];
  if (!Array.isArray(rows)) return out;
  for (const row of rows) {
    const s = toStringInput(row.sector).toLowerCase();
    if (!s || !SECTOR_ID_RE.test(s) || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

function buildInsightFromDigestRow(row, sectorFallback) {
  const full = row.full_digest != null && String(row.full_digest).trim() ? String(row.full_digest) : "";
  const short = row.output_text != null && String(row.output_text).trim() ? String(row.output_text) : "";
  const text = full || short;
  return {
    run_id: row.run_id || null,
    sector: row.sector || sectorFallback,
    recorded_at: row.recorded_at || null,
    text,
  };
}

async function fetchLatestInsight(env, sector) {
  const base = String(env.SUPABASE_URL || "").trim().replace(/\/+$/, "");
  const key = String(env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
  const sev = encodeURIComponent(sector);
  const url =
    `${base}/rest/v1/llm_digest_memory?sector=eq.${sev}` +
    "&select=run_id,sector,output_text,full_digest,recorded_at&order=recorded_at.desc&limit=1";
  const res = await fetch(url, {
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      Accept: "application/json",
    },
  });
  const txt = await res.text();
  if (!res.ok) {
    throw new Error(`Supabase REST ${res.status}: ${txt.slice(0, 500)}`);
  }
  let rows;
  try {
    rows = txt ? JSON.parse(txt) : [];
  } catch (_e) {
    throw new Error("Supabase REST returned non-JSON");
  }
  if (!Array.isArray(rows) || !rows.length) {
    return { ok: true, insight: null };
  }
  const row = rows[0];
  const insight = buildInsightFromDigestRow(row, sector);
  return { ok: true, insight };
}

async function supabaseSelectDigestRows(base, key, queryPath) {
  const url = `${base}/rest/v1/llm_digest_memory${queryPath}`;
  const res = await fetch(url, {
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      Accept: "application/json",
    },
  });
  const txt = await res.text();
  if (!res.ok) {
    throw new Error(`Supabase REST ${res.status}: ${txt.slice(0, 500)}`);
  }
  try {
    return txt ? JSON.parse(txt) : [];
  } catch (_e) {
    throw new Error("Supabase REST returned non-JSON");
  }
}

/**
 * Latest digest row for a GitHub Actions run id + sector (github_run_id set by CI after upgrade).
 */
async function fetchInsightByGithubRun(env, ghRunId, sectorParam) {
  const base = String(env.SUPABASE_URL || "").trim().replace(/\/+$/, "");
  const key = String(env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
  if (!base || !key) {
    throw new Error("Missing worker secrets: SUPABASE_URL and/or SUPABASE_SERVICE_ROLE_KEY");
  }
  const run = await gh(env, `/actions/runs/${ghRunId}`);
  const wf = (run.path || "").split("/").pop() || "";
  if (wf !== "run_titan_now.yml") {
    throw new Error("That GitHub run is not “Run Titan Now”.");
  }
  const inputs = normalizeWorkflowInputs(run.inputs);
  let mode = toStringInput(inputs.mode).toLowerCase();
  const meta = {
    github_run_id: ghRunId,
    github_run_number: run.run_number ?? null,
    workflow_mode: mode,
    workflow_status: run.status || null,
    workflow_conclusion: run.conclusion || null,
  };

  const gid = encodeURIComponent(String(ghRunId));
  const rowsForRun = await supabaseSelectDigestRows(
    base,
    key,
    `?github_run_id=eq.${gid}` +
      "&select=run_id,sector,output_text,full_digest,recorded_at,github_run_id&order=recorded_at.desc&limit=32",
  );
  const sectorsFromDb = uniqueSectorsFromDigestRows(rowsForRun);

  if (mode === "portfolio" || mode === "live") {
    return {
      ok: true,
      insight: null,
      ...meta,
      note: "This workflow mode does not store a sector digest in llm_digest_memory.",
    };
  }

  let sector = toStringInput(sectorParam).toLowerCase();
  if (!sector) sector = toStringInput(inputs.sector_id).toLowerCase();
  if (!sector && sectorsFromDb.length === 1) sector = sectorsFromDb[0];

  if (!mode) {
    if (sectorsFromDb.length > 1) mode = "all_sectors";
    else if (sectorsFromDb.length === 1) mode = "sector";
    else if (toStringInput(inputs.custom_symbols)) mode = "custom";
    else if (toStringInput(inputs.sector_id)) mode = "sector";
  }
  if (mode) meta.workflow_mode = mode;

  if (mode === "all_sectors" && (!sector || !SECTOR_ID_RE.test(sector))) {
    if (sectorsFromDb.length > 1) {
      return {
        ok: false,
        code: "sector_required",
        error: "This run has multiple saved sector digests. Pass sector= query param.",
        available_sectors: sectorsFromDb,
        ...meta,
      };
    }
    return {
      ok: false,
      code: "sector_required",
      error: "This run is all_sectors mode. Pass sector= query param (sector to show).",
      available_sectors: sectorsFromDb,
      ...meta,
    };
  }
  if (!sector || !SECTOR_ID_RE.test(sector)) {
    if (sectorsFromDb.length > 1) {
      return {
        ok: false,
        code: "sector_required",
        error:
          "GitHub did not return workflow inputs for this run. Pick a sector (saved digests: " +
          sectorsFromDb.join(", ") +
          ").",
        available_sectors: sectorsFromDb,
        ...meta,
      };
    }
    return {
      ok: false,
      code: "bad_sector",
      error: "Could not determine a valid sector for this run.",
      available_sectors: sectorsFromDb,
      note:
        sectorsFromDb.length === 0
          ? "No llm_digest_memory row with this github_run_id yet (or column not populated for this run)."
          : "",
      ...meta,
    };
  }

  const sev = encodeURIComponent(sector);
  const queryPath =
    `?github_run_id=eq.${gid}&sector=eq.${sev}` +
    "&select=run_id,sector,output_text,full_digest,recorded_at,github_run_id&order=recorded_at.desc&limit=1";
  let rows = await supabaseSelectDigestRows(base, key, queryPath);
  if (!Array.isArray(rows) || !rows.length) {
    const latest = await fetchLatestInsight(env, sector);
    if (latest.insight && String(latest.insight.text || "").trim()) {
      return {
        ok: true,
        insight: latest.insight,
        ...meta,
        sector,
        note:
          "No row linked to this GitHub run_id in llm_digest_memory. Showing the latest digest for this sector instead. " +
          "Run Titan Now must use TITAN_ENABLE_ANALYSIS_STORE=1 (see run_titan_now.yml) so new jobs write rows with github_run_id.",
      };
    }
    return {
      ok: true,
      insight: null,
      ...meta,
      sector,
      note:
        "llm_digest_memory has no rows for this GitHub run and sector (table empty or persist disabled). " +
        "Set GitHub Actions env TITAN_ENABLE_ANALYSIS_STORE=1, confirm sql/create_analysis_rollups.sql + alter scripts ran, then run a new sector/custom digest.",
    };
  }
  const row = rows[0];
  const insight = buildInsightFromDigestRow(row, sector);
  if (!String(insight.text || "").trim()) {
    return {
      ok: true,
      insight: null,
      ...meta,
      sector,
      note: "A digest row exists for this run but full_digest/output_text are empty.",
    };
  }
  return { ok: true, insight, ...meta, sector };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return json({ ok: true }, 204);

    try {
      const url = new URL(request.url);
      const path = (url.pathname || "/").replace(/\/+$/, "") || "/";

      const isGetLike = request.method === "GET" || request.method === "HEAD";

      if (isGetLike && path === "/breeze-login") {
        const key = env.BREEZE_API_KEY ? String(env.BREEZE_API_KEY).trim() : "";
        if (!key) {
          return json({ error: "BREEZE_API_KEY not configured on proxy" }, 501);
        }
        const target = `https://api.icicidirect.com/apiuser/login?api_key=${encodeURIComponent(key)}`;
        return Response.redirect(target, 302);
      }

      if (request.method === "POST" && path === "/dispatch") {
        const body = await request.json();
        const workflow = String(body.workflow || "").trim();
        const ref = String(body.ref || "main").trim();
        const rawInputs = body.inputs && typeof body.inputs === "object" ? body.inputs : {};

        if (!ALLOWED_WORKFLOWS.has(workflow)) {
          return json({ error: "workflow not allowed" }, 400);
        }
        let inputs;
        try {
          inputs = sanitizeDispatchPayload(workflow, rawInputs);
        } catch (e) {
          return json({ error: String(e.message || e) }, 400);
        }
        await gh(
          env,
          `/actions/workflows/${workflow}/dispatches`,
          "POST",
          { ref, inputs },
        );
        return json({ ok: true, workflow, ref });
      }

      if (isGetLike && path === "/runs") {
        const limit = Number(url.searchParams.get("limit") || "20");
        const data = await gh(env, `/actions/runs?per_page=${Math.max(1, Math.min(limit, 100))}`);
        return json(data);
      }

      const workflowRunMatch = path.match(/^\/workflow-run\/(\d+)$/);
      if (isGetLike && workflowRunMatch) {
        const runId = workflowRunMatch[1];
        const data = await gh(env, `/actions/runs/${runId}`);
        const inputs = data.inputs && typeof data.inputs === "object" ? data.inputs : {};
        const safe = {
          id: data.id,
          name: data.name,
          status: data.status,
          conclusion: data.conclusion,
          html_url: data.html_url,
          run_number: data.run_number,
          created_at: data.created_at,
          updated_at: data.updated_at,
          display_title: data.display_title,
          path: data.path,
          inputs,
        };
        return json(safe);
      }

      if (isGetLike && path === "/health") {
        const supabaseUrl = Boolean(String(env.SUPABASE_URL || "").trim());
        const supabaseKey = Boolean(String(env.SUPABASE_SERVICE_ROLE_KEY || "").trim());
        return json({
          ok: true,
          repo: `${env.REPO_OWNER || "<missing>"}/${env.REPO_NAME || "<missing>"}`,
          has_pat: Boolean(env.GITHUB_PAT),
          allowed_workflows: Array.from(ALLOWED_WORKFLOWS),
          has_supabase_insights: supabaseUrl && supabaseKey,
        });
      }

      const ghRunInsightMatch = path.match(/^\/insights\/github-run\/(\d{1,20})$/);
      if (isGetLike && ghRunInsightMatch) {
        const ghRunNumericId = ghRunInsightMatch[1];
        const sectorQ = toStringInput(url.searchParams.get("sector")).toLowerCase();
        const data = await fetchInsightByGithubRun(env, ghRunNumericId, sectorQ);
        if (data && data.ok === false) {
          return json(data, 400);
        }
        return json(data);
      }

      if (isGetLike && path === "/insights/latest") {
        const sector = toStringInput(url.searchParams.get("sector")).toLowerCase();
        if (!sector || !SECTOR_ID_RE.test(sector)) {
          return json({ error: "sector query param is required ([a-z0-9_]{1,64})" }, 400);
        }
        const base = String(env.SUPABASE_URL || "").trim();
        const key = String(env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
        if (!base || !key) {
          return json(
            {
              ok: false,
              code: "missing_supabase_secrets",
              error:
                "Set Worker secrets SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on titan-proxy (Dashboard → Workers → titan-proxy → Settings → Variables, or: npx wrangler secret put SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from repo root). Then redeploy or save.",
            },
            503,
          );
        }
        const data = await fetchLatestInsight(env, sector);
        return json(data);
      }

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500);
    }
  },
};
