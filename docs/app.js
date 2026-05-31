const WORKFLOWS = {
  runTitan: "run_titan_now.yml",
  fetchSymbolNews: "news_fetch.yml",
  runReconcile: "daily_post_market_reconcile.yml",
  validate: "validate_breeze_token_manual.yml",
  persist: "persist_breeze_token_manual.yml",
  refreshRankings: "refresh_sector_rankings_weekly.yml",
};
const PROXY_BASE = "https://titan-proxy.arunjain-real.workers.dev";
const STATIC_SECTOR_OPTIONS = [
  "ai",
  "auto",
  "auto_ancillary",
  "banks_psu",
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
];
const EXCLUDED_DYNAMIC_SECTOR_IDS = new Set(["unknown", "non_equity"]);
const RUN_MODES = new Set(["sector", "all_sectors", "custom"]);
const TITAN_SCOPES = new Set(["full", "priority"]);
/** Dispatched priority runs always use top 10 from sector_priority_rankings (single sector and all_sectors). */
const PRIORITY_TOP_N_FIXED = "10";
/** Portfolio quick scan: max holdings sent and evaluated per run (matches main.py / workflow default). */
const PORTFOLIO_MAX_POSITIONS_FIXED = 75;
const EXCHANGE_OPTIONS = new Set(["NSE", "BSE"]);
const CUSTOM_SYMBOL_TOKEN_RE = /^[A-Z0-9&._-]{1,25}$/;
const MAX_CUSTOM_SYMBOLS = 120;
const COMMON_NON_SYMBOLS = new Set([
  "HOLDINGS",
  "HOLDING",
  "SYMBOL",
  "COMPANY",
  "QUANTITY",
  "QTY",
  "PRICE",
  "VALUE",
  "TOTAL",
  "AVG",
  "AVERAGE",
  "COST",
  "INVESTED",
  "UNITS",
  "UNIT",
  "ISIN",
  "PORTFOLIO",
]);

const el = (id) => document.getElementById(id);
const statusEl = el("status");

function setStatus(msg) {
  statusEl.textContent = msg;
}

function setWorking(label) {
  setStatus(`Working: ${label} ...`);
}

function normalizeProxyBase(raw = PROXY_BASE) {
  const trimmed = String(raw || PROXY_BASE).trim();
  if (!trimmed) throw new Error("Proxy URL is required.");
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch (_e) {
    throw new Error("Proxy URL is invalid. Use a full URL like https://your-proxy.example.com");
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("Proxy URL must start with http:// or https://");
  }
  parsed.search = "";
  parsed.hash = "";
  const parts = parsed.pathname.split("/").filter(Boolean);
  const knownApiSuffixes = new Set([
    "health",
    "runs",
    "dispatch",
    "insights",
    "latest",
    "workflow-run",
    "github-run",
    "news",
    "status",
    "refresh",
    "sectors",
    "active",
  ]);
  while (parts.length && knownApiSuffixes.has(parts[parts.length - 1].toLowerCase())) {
    parts.pop();
  }
  while (parts.length && /^\d{1,20}$/.test(parts[parts.length - 1])) {
    parts.pop();
  }
  parsed.pathname = parts.length ? `/${parts.join("/")}` : "";
  return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
}

function cfg() {
  const proxyBase = normalizeProxyBase(PROXY_BASE);
  return { proxyBase };
}

function setSectorModeUi(mode) {
  const sectorEl = el("sectorId");
  const hintEl = el("sectorHint");
  const customSymbolsEl = el("customSymbols");
  const customSymbolsHintEl = el("customSymbolsHint");
  if (!sectorEl) return;
  const isSectorMode = mode === "sector";
  const isCustomMode = mode === "custom";
  sectorEl.disabled = !isSectorMode;
  if (customSymbolsEl) {
    customSymbolsEl.disabled = !isCustomMode;
  }
  if (hintEl) {
    hintEl.textContent = isSectorMode
      ? "Used only when mode=sector."
      : "Ignored for selected mode.";
  }
  if (customSymbolsHintEl) {
    customSymbolsHintEl.textContent = isCustomMode
      ? "Used only when mode=custom (NSE)."
      : "Ignored for selected mode.";
  }
  const titanScopeEl = el("titanScope");
  const titanScopeHintEl = el("titanScopeHint");
  const isSectorOrAll = mode === "sector" || mode === "all_sectors";
  if (titanScopeEl) {
    titanScopeEl.disabled = !isSectorOrAll;
  }
  if (titanScopeHintEl) {
    titanScopeHintEl.textContent = isSectorOrAll
      ? "Uses sector_priority_rankings; weekly refresh on Saturdays. Priority mode uses top 10."
      : "Not used for this mode.";
  }
}

/** Matches Worker SECTOR_ID_RE for workflow + Supabase sector keys. */
const SECTOR_INPUT_RE = /^[a-z0-9_]{1,64}$/;

function buildSectorOptions(rawSectors) {
  const source = Array.isArray(rawSectors) ? rawSectors : [];
  const out = [];
  const seen = new Set();
  for (const sector of source) {
    const sid = String(sector || "").trim().toLowerCase();
    if (!sid || !SECTOR_INPUT_RE.test(sid) || EXCLUDED_DYNAMIC_SECTOR_IDS.has(sid)) continue;
    if (seen.has(sid)) continue;
    seen.add(sid);
    out.push(sid);
  }
  return out;
}

function renderSectorOptions(selectId, sectorOptions, preferred = "defence") {
  const sectorEl = el(selectId);
  if (!sectorEl) return false;
  const available = buildSectorOptions(sectorOptions);
  if (!available.length) return false;
  const previous = String(sectorEl.value || "").trim().toLowerCase();
  sectorEl.innerHTML = "";
  for (const sid of available) {
    const opt = document.createElement("option");
    opt.value = sid;
    opt.textContent = sid;
    sectorEl.appendChild(opt);
  }
  const nextValue =
    (previous && available.includes(previous) && previous) ||
    (preferred && available.includes(preferred) && preferred) ||
    available[0];
  sectorEl.value = nextValue;
  return true;
}

async function fetchActiveSectorsFromProxy() {
  const data = await ghApi("/sectors/active");
  if (!data || data.ok !== true || !Array.isArray(data.sectors)) {
    throw new Error("Invalid /sectors/active response.");
  }
  const sectors = buildSectorOptions(data.sectors);
  if (!sectors.length) {
    throw new Error("Proxy returned empty sector list.");
  }
  return sectors;
}

function warnDynamicSectorFallback(selectId, reason) {
  const msg = String(reason || "unknown error");
  console.warn(`[Titan UI] Dynamic sector load failed for #${selectId}. Using static defaults.`, msg);
  const current = String(statusEl?.textContent || "").trim();
  if (
    statusEl &&
    (current === "" || current === "UI ready." || current === "Loading UI…" || current === "Loading…")
  ) {
    setStatus("UI ready.\nSector list fallback active (static defaults).");
  }
}

async function initSectorOptions(selectId = "sectorId") {
  const hasStatic = renderSectorOptions(selectId, STATIC_SECTOR_OPTIONS);
  if (!hasStatic) return;
  try {
    const dynamicSectors = await fetchActiveSectorsFromProxy();
    renderSectorOptions(selectId, dynamicSectors);
  } catch (e) {
    warnDynamicSectorFallback(selectId, e.message || e);
  }
}

function classifyProxyError(status, responseText) {
  const body = String(responseText || "");
  if (status === 503 && (body.includes("missing_supabase") || body.includes("SUPABASE_"))) {
    return "Supabase not configured on the Worker. Set secrets SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on titan-proxy (see docs/PROXY_SETUP.md).";
  }
  if (status === 404) {
    return (
      "404 from proxy endpoint.\n" +
      "This URL does not expose Titan API routes (/health, /dispatch, /runs, /workflow-run/{id}, /insights/latest, /insights/github-run/{id}, /news/status, /news/refresh, /sectors/active).\n" +
      "Use the backend Worker URL, not the UI page URL."
    );
  }
  if (status === 401) {
    return "401 from GitHub API via proxy. Rotate Worker secret GITHUB_PAT.";
  }
  if (status === 403) {
    return (
      "403 from GitHub API via proxy. Check PAT scopes and repo access " +
      "(Actions write, Contents read/write, Metadata read)."
    );
  }
  if (body.includes("workflow not allowed")) {
    return "Workflow filename is blocked by proxy ALLOWED_WORKFLOWS.";
  }
  return "";
}

function validateBreezeTokenInputForPersist(rawValue) {
  const tokenInput = String(rawValue || "").trim();
  if (!tokenInput) throw new Error("Token input is required.");
  if (tokenInput.length < 8) {
    throw new Error("Token input looks too short. Paste full API_Session or full redirect URL.");
  }
  if (tokenInput.includes("\n") || tokenInput.includes("\r")) {
    throw new Error("Token input must be single-line text (no newlines).");
  }
  if (
    tokenInput.length >= 2 &&
    (
      (tokenInput.startsWith('"') && tokenInput.endsWith('"')) ||
      (tokenInput.startsWith("'") && tokenInput.endsWith("'"))
    )
  ) {
    throw new Error("Token input appears wrapped in quotes. Paste raw API_Session or full redirect URL.");
  }
  return tokenInput;
}

async function ghApi(path, method = "GET", body = null) {
  const { proxyBase } = cfg();
  const url = `${proxyBase}${path}`;
  const res = await fetch(url, {
    method,
    headers: {
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const txt = await res.text();
    let bodyMsg = txt;
    try {
      const j = JSON.parse(txt);
      if (j && typeof j.error === "string") {
        bodyMsg = j.error + (j.code ? ` [${j.code}]` : "");
      }
    } catch (_e) {
      /* keep raw txt */
    }
    const hint = classifyProxyError(res.status, txt);
    const hintBlock = hint ? `\nHint: ${hint}\n` : "\n";
    throw new Error(`${res.status} ${res.statusText}${hintBlock}${bodyMsg}`);
  }
  if (res.status === 204) return null;
  return await res.json();
}

async function checkConnection({ showSuccess = false } = {}) {
  const health = await ghApi("/health");
  if (!health || health.ok !== true) {
    throw new Error("Proxy health response is invalid.");
  }
  if (showSuccess) {
    const flows = Array.isArray(health.allowed_workflows) ? health.allowed_workflows.join(", ") : "n/a";
    const insights = health.has_supabase_insights === true ? "yes" : "no";
    const digestRows =
      health.digest_memory_rows != null ? String(health.digest_memory_rows) : "unknown";
    const digestErr = health.digest_memory_count_error
      ? `\nDigest table count error: ${health.digest_memory_count_error}`
      : "";
    setStatus(
      `Connection OK\nProxy repo: ${health.repo}\nPAT configured: ${Boolean(health.has_pat)}\n` +
        `Supabase insights: ${insights}\n` +
        `llm_digest_memory rows (proxy): ${digestRows}${digestErr}\n` +
        `Allowed workflows: ${flows}`,
    );
  }
  return health;
}

async function dispatchWorkflow(filename, inputs = {}, statusSuffix = "", ref = "main") {
  await ghApi("/dispatch", "POST", { workflow: filename, ref, inputs });
  const extra = statusSuffix ? `\n\n${statusSuffix}` : "";
  setStatus(`Dispatched ${filename} successfully.${extra}`);
}

function humanizeAgeMinutes(age) {
  const n = Number(age);
  if (!Number.isFinite(n) || n < 0) return null;
  if (n < 1) return "just now";
  if (n < 60) return `${Math.round(n)} min ago`;
  const hrs = Math.floor(n / 60);
  const mins = Math.round(n % 60);
  if (mins === 0) return `${hrs}h ago`;
  return `${hrs}h ${mins}m ago`;
}

function renderGlobalNewsFreshness(statusPayload) {
  const host = el("globalNewsFreshness");
  if (!host) return;
  const ttl = Number(statusPayload?.ttl_hours || 2);
  const age = statusPayload?.age_minutes;
  const snap = statusPayload?.snapshot || null;
  const fresh = statusPayload?.fresh === true;
  const chips = [];
  if (!snap) {
    chips.push('<span class="chip chip-stale">No snapshot</span>');
    chips.push(`<span class="chip chip-muted">TTL ${ttl}h</span>`);
    host.innerHTML = chips.join("");
    host.title = "No global news snapshot yet. Tap Global news to refresh.";
    return;
  }
  const ageLabel = humanizeAgeMinutes(age);
  const refreshed = snap.refreshed_at || "";
  const itemCount = snap.item_count ?? "?";
  const fetchStatus = String(snap.fetch_status || "unknown").toLowerCase();
  chips.push(
    fresh
      ? '<span class="chip chip-fresh">Fresh</span>'
      : '<span class="chip chip-stale">Stale</span>',
  );
  if (ageLabel) {
    chips.push(`<span class="chip chip-muted">${escapeHtml(ageLabel)}</span>`);
  }
  chips.push(`<span class="chip chip-muted">${escapeHtml(String(itemCount))} items</span>`);
  if (fetchStatus && fetchStatus !== "ok") {
    chips.push(`<span class="chip chip-error">${escapeHtml(fetchStatus)}</span>`);
  }
  host.innerHTML = chips.join("");
  const titleParts = [`Refreshed ${refreshed || "n/a"}`, `TTL ${ttl}h`];
  if (fetchStatus) titleParts.push(`Status ${fetchStatus}`);
  host.title = titleParts.join(" · ");
}

async function fetchGlobalNewsStatus() {
  const data = await ghApi("/news/status");
  if (!data || data.ok !== true) {
    throw new Error("Unexpected response from /news/status.");
  }
  renderGlobalNewsFreshness(data);
  return data;
}

async function refreshGlobalNewsSnapshot() {
  const data = await ghApi("/news/refresh", "POST", {});
  if (!data || data.ok !== true) {
    throw new Error("Unexpected response from /news/refresh.");
  }
  const status = await fetchGlobalNewsStatus();
  return { refresh: data, status };
}

function parseCustomSymbols(raw) {
  const chunks = String(raw || "")
    .toUpperCase()
    .split(/[\s,;\n\r\t]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const deduped = [];
  const seen = new Set();
  for (const sym of chunks) {
    if (!CUSTOM_SYMBOL_TOKEN_RE.test(sym)) {
      throw new Error(
        `Invalid custom symbol "${sym}". Use A-Z, 0-9, &, ., _, - (max 25 chars).`,
      );
    }
    if (!seen.has(sym)) {
      seen.add(sym);
      deduped.push(sym);
    }
  }
  if (!deduped.length) {
    throw new Error("Custom mode requires at least one symbol.");
  }
  if (deduped.length > MAX_CUSTOM_SYMBOLS) {
    throw new Error(`Custom mode supports up to ${MAX_CUSTOM_SYMBOLS} symbols per run.`);
  }
  return deduped;
}

function isLikelyPortfolioSymbol(symbol) {
  const sym = String(symbol || "").toUpperCase();
  if (!sym) return false;
  if (COMMON_NON_SYMBOLS.has(sym)) return false;
  // Keep tokens that contain letters; reject pure numeric/price fragments.
  if (!/[A-Z]/.test(sym)) return false;
  return CUSTOM_SYMBOL_TOKEN_RE.test(sym);
}

function normalizeHoldingSymbol(raw) {
  return String(raw || "")
    .toUpperCase()
    .trim()
    .replace(/[^A-Z0-9&._-]/g, "");
}

function parseSymbolWithExchange(token, defaultExchange = "NSE") {
  const raw = String(token || "").toUpperCase().trim();
  if (!raw) return null;
  for (const sep of [":", "-", "/"]) {
    if (raw.includes(sep)) {
      const [left, right] = raw.split(sep, 2).map((x) => x.trim());
      const leftNorm = normalizeHoldingSymbol(left);
      const rightNorm = normalizeHoldingSymbol(right);
      if (EXCHANGE_OPTIONS.has(leftNorm) && isLikelyPortfolioSymbol(rightNorm)) {
        return { symbol: rightNorm, exchange: leftNorm };
      }
      if (EXCHANGE_OPTIONS.has(rightNorm) && isLikelyPortfolioSymbol(leftNorm)) {
        return { symbol: leftNorm, exchange: rightNorm };
      }
    }
  }
  const sym = normalizeHoldingSymbol(raw);
  if (!isLikelyPortfolioSymbol(sym)) return null;
  return { symbol: sym, exchange: defaultExchange };
}

function _parseNumericToken(raw) {
  const t = String(raw || "")
    .replace(/,/g, "")
    .trim();
  if (!/^[+-]?\d+(?:\.\d+)?$/.test(t)) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  return n;
}

function parsePortfolioRowsFromText(rawText, defaultExchange = "NSE") {
  const lines = String(rawText || "").split(/\r?\n/);
  const holdings = [];
  const rejected = [];
  for (const line of lines) {
    const cleaned = String(line || "").trim();
    if (!cleaned || cleaned.startsWith("#")) continue;
    let tokens = cleaned.split(/[,\t|;]+/).map((t) => t.trim()).filter(Boolean);
    if (tokens.length === 1) {
      tokens = cleaned.split(/\s+/).map((t) => t.trim()).filter(Boolean);
    }
    if (!tokens.length) continue;
    const firstColRaw = tokens[0];
    const parsed = parseSymbolWithExchange(firstColRaw, defaultExchange);
    if (!parsed) {
      rejected.push({ token: firstColRaw, reason: "invalid_first_column_symbol" });
      continue;
    }
    const nums = tokens.slice(1).map(_parseNumericToken).filter((n) => n !== null);
    if (!nums.length || Number(nums[0]) === 0) {
      rejected.push({ token: cleaned, reason: "missing_or_zero_quantity" });
      continue;
    }
    const quantity = Number(nums[0]);
    const avgBuy = nums.length >= 2 && Number(nums[1]) > 0 ? Number(nums[1]) : null;
    holdings.push({
      symbol: parsed.symbol,
      exchange: parsed.exchange,
      quantity,
      avg_buy_price: avgBuy,
    });
  }
  return {
    holdings: holdings.slice(0, PORTFOLIO_MAX_POSITIONS_FIXED),
    rejected,
  };
}

function _groupPdfItemsByRow(items, tolerance = 2) {
  const rows = [];
  for (const item of items || []) {
    const text = String(item?.str || "").trim();
    if (!text) continue;
    const tr = Array.isArray(item?.transform) ? item.transform : [];
    const x = Number(tr[4] || 0);
    const y = Number(tr[5] || 0);
    let row = null;
    for (const existing of rows) {
      if (Math.abs(existing.y - y) <= tolerance) {
        row = existing;
        break;
      }
    }
    if (!row) {
      row = { y, cells: [] };
      rows.push(row);
    }
    row.cells.push({ x, text });
  }
  return rows;
}

function _parseFirstColumnTokenFromRow(cells, defaultExchange) {
  if (!Array.isArray(cells) || !cells.length) {
    return { parsed: null, token: "", sorted: [] };
  }
  const sorted = [...cells].sort((a, b) => a.x - b.x);
  const first = String(sorted[0]?.text || "").trim();
  const second = String(sorted[1]?.text || "").trim();
  const candidates = [first];
  if (second) {
    candidates.push(`${first}${second}`);
    candidates.push(`${first} ${second}`);
  }
  for (const cand of candidates) {
    const parsed = parseSymbolWithExchange(cand, defaultExchange);
    if (parsed) {
      return { parsed, token: cand, sorted };
    }
  }
  return { parsed: null, token: first, sorted };
}

async function extractPortfolioRowsFromPdf(file, defaultExchange = "NSE") {
  const pdfjsLib = await import("https://cdn.jsdelivr.net/npm/pdfjs-dist@4.5.136/build/pdf.mjs");
  if (pdfjsLib && pdfjsLib.GlobalWorkerOptions) {
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.5.136/build/pdf.worker.mjs";
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  const pdf = await pdfjsLib.getDocument({ data: bytes }).promise;
  const holdings = [];
  const rejected = [];
  for (let i = 1; i <= pdf.numPages; i += 1) {
    const page = await pdf.getPage(i);
    const tc = await page.getTextContent();
    const rows = _groupPdfItemsByRow(tc.items || []);
    for (const row of rows) {
      const { parsed, token, sorted } = _parseFirstColumnTokenFromRow(row.cells, defaultExchange);
      if (!parsed) {
        if (token) {
          rejected.push({ token, reason: "invalid_first_column_symbol" });
        }
        continue;
      }
      const nums = sorted
        .slice(1)
        .map((cell) => _parseNumericToken(cell.text))
        .filter((n) => n !== null);
      if (!nums.length || Number(nums[0]) === 0) {
        rejected.push({ token: token || parsed.symbol, reason: "missing_or_zero_quantity" });
        continue;
      }
      const quantity = Number(nums[0]);
      const avgBuy = nums.length >= 2 && Number(nums[1]) > 0 ? Number(nums[1]) : null;
      holdings.push({
        symbol: parsed.symbol,
        exchange: parsed.exchange,
        quantity,
        avg_buy_price: avgBuy,
      });
    }
  }
  return {
    holdings: holdings.slice(0, PORTFOLIO_MAX_POSITIONS_FIXED),
    rejected,
  };
}

async function buildPortfolioSymbolsPayload() {
  const exchange = "NSE";
  const pasted = String(el("portfolioHoldingsText")?.value || "");
  const pdfFile = el("portfolioPdfFile")?.files?.[0] || null;
  const accepted = [];
  const rejected = [];
  const merged = new Map();
  const addAccepted = (list) => {
    for (const row of list || []) {
      const symbol = String(row.symbol || "").toUpperCase();
      const exch = String(row.exchange || exchange).toUpperCase();
      const quantity = Number(row.quantity || 0);
      const avgBuy =
        row.avg_buy_price !== null && row.avg_buy_price !== undefined ? Number(row.avg_buy_price) : null;
      if (!symbol || !Number.isFinite(quantity) || quantity === 0) continue;
      const key = `${exch}:${symbol}`;
      if (!merged.has(key)) {
        merged.set(key, { symbol, exchange: exch, quantity: 0, cost: 0, qtyCost: 0 });
      }
      const rec = merged.get(key);
      rec.quantity += quantity;
      if (avgBuy !== null && Number.isFinite(avgBuy) && avgBuy > 0) {
        const qabs = Math.abs(quantity);
        rec.cost += qabs * avgBuy;
        rec.qtyCost += qabs;
      }
    }
    for (const rec of merged.values()) {
      if (accepted.length >= PORTFOLIO_MAX_POSITIONS_FIXED) {
        break;
      }
      accepted.push({
        symbol: rec.symbol,
        exchange: rec.exchange,
        quantity: Number(rec.quantity.toFixed(4)),
        avg_buy_price: rec.qtyCost > 0 ? Number((rec.cost / rec.qtyCost).toFixed(4)) : null,
      });
    }
  };

  if (pdfFile) {
    try {
      const pdfResult = await extractPortfolioRowsFromPdf(pdfFile, exchange);
      addAccepted(pdfResult.holdings);
      rejected.push(...(pdfResult.rejected || []));
    } catch (e) {
      throw new Error(`PDF extraction failed. ${e.message || e}`);
    }
  }
  if (pasted.trim()) {
    const txtResult = parsePortfolioRowsFromText(pasted, exchange);
    addAccepted(txtResult.holdings);
    rejected.push(...(txtResult.rejected || []));
  }
  if (!accepted.length) {
    throw new Error(
      "No holdings parsed from first column of PDF/text. Expected: SYMBOL, QTY, BUY_PRICE (buy price optional).",
    );
  }
  const rejectionPreview = rejected
    .slice(0, 8)
    .map((r) => `${r.token} (${r.reason})`)
    .join(", ");
  return {
    mode: "portfolio",
    custom_exchange: exchange,
    portfolio_holdings_json: JSON.stringify(accepted),
    portfolio_max_positions: String(PORTFOLIO_MAX_POSITIONS_FIXED),
    parsed_count: accepted.length,
    parsed_symbols_preview: accepted
      .slice(0, 12)
      .map((x) => `${x.symbol}[q=${x.quantity}${x.avg_buy_price ? `,buy=${x.avg_buy_price}` : ""}]`)
      .join(", "),
    rejected_count: rejected.length,
    rejected_preview: rejectionPreview,
  };
}

function buildRunTitanInputs() {
  const mode = (el("runMode")?.value || "sector").trim();
  if (!RUN_MODES.has(mode)) {
    throw new Error("Mode is invalid.");
  }
  const sectorId = (el("sectorId")?.value || "").trim();

  const inputs = {
    mode,
    sector_id: sectorId,
    titan_scope: "",
    priority_top_n: "",
  };

  if (mode === "sector" && !sectorId) {
    throw new Error("Sector ID is required for sector mode.");
  }
  if (mode === "sector" || mode === "all_sectors") {
    const ts = String(el("titanScope")?.value || "priority").trim().toLowerCase();
    if (!TITAN_SCOPES.has(ts)) {
      throw new Error("Titan scope must be full or priority.");
    }
    inputs.titan_scope = ts;
    if (ts === "priority") {
      inputs.priority_top_n = PRIORITY_TOP_N_FIXED;
    }
  }

  if (mode === "custom") {
    const parsed = parseCustomSymbols(el("customSymbols")?.value || "");
    const customSectorId = sectorId || "custom_ui";
    inputs.sector_id = customSectorId;
    inputs.custom_symbols = parsed.join(",");
    inputs.custom_exchange = "NSE";
  }

  return inputs;
}

function buildReconcileInputs() {
  const scope = String(el("reconcileScope")?.value || "sector").trim().toLowerCase();
  if (!["all-stocks", "sector"].includes(scope)) {
    throw new Error("Reconcile scope is invalid.");
  }
  const sector = String(el("reconcileSectorId")?.value || "").trim().toLowerCase();
  const workers = String(el("reconcileWorkers")?.value || "").trim();
  const backfillDays = String(el("reconcileBackfillDays")?.value || "").trim();
  const inputs = {
    scope,
    sector_id: "",
    workers: "",
    backfill_days: "",
    backfill_only: "false",
  };
  if (scope === "sector") {
    if (!sector || !SECTOR_INPUT_RE.test(sector)) {
      throw new Error("Reconcile sector is required for scope=sector.");
    }
    inputs.sector_id = sector;
  }
  if (workers) {
    if (!/^\d+$/.test(workers)) {
      throw new Error("Reconcile workers must be numeric.");
    }
    inputs.workers = workers;
  }
  if (backfillDays) {
    if (!/^\d+$/.test(backfillDays)) {
      throw new Error("Backfill days must be numeric.");
    }
    inputs.backfill_days = backfillDays;
  }
  return inputs;
}

async function fetchInsightTextForSector(sectorId) {
  const sid = String(sectorId || "").trim().toLowerCase();
  if (!sid || !SECTOR_INPUT_RE.test(sid)) return null;
  const data = await ghApi(`/insights/latest?sector=${encodeURIComponent(sid)}`);
  if (!data || data.ok !== true) {
    throw new Error("Unexpected response from /insights/latest.");
  }
  const insight = data.insight;
  if (!insight || !String(insight.text || "").trim()) return null;
  return insight;
}

function renderInsightDigestHtml(digestText) {
  if (window.TitanDigestRender) {
    window.TitanDigestRender.renderInsightDigestHtml(digestText);
    return;
  }
  const host = el("insightDigestHtml");
  if (host) host.textContent = String(digestText || "");
}

function escapeHtml(s) {
  if (window.TitanDigestRender) return window.TitanDigestRender.escapeHtml(s);
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInsightMeta(runMeta, insight, sectorLabel) {
  if (window.TitanDigestRender) {
    window.TitanDigestRender.renderInsightMeta(runMeta, insight, sectorLabel);
  }
}

function clearInsightDigestView() {
  const htmlHost = el("insightDigestHtml");
  if (htmlHost) htmlHost.innerHTML = "";
  const metaHost = el("insightDigestMeta");
  if (metaHost) metaHost.innerHTML = "";
}

function applyInsightToTextarea(textareaEl, insight, sectorLabel, runMeta = {}) {
  if (!textareaEl || !insight) return;
  let head = "";
  if (runMeta.github_run_number != null) {
    head += `GitHub Run Titan #${runMeta.github_run_number} (workflow run id ${runMeta.github_run_id ?? "?"})\n`;
  }
  if (runMeta.workflow_mode) {
    head += `Workflow mode: ${runMeta.workflow_mode}\n`;
  }
  if (head) head += "\n";
  const when = insight.recorded_at ? `Recorded: ${insight.recorded_at}\n` : "";
  const lab = sectorLabel || insight.sector || "";
  const body = `${head}${when}Sector: ${lab}\nDigest run_id: ${insight.run_id || "n/a"}\n\n${insight.text || ""}`;
  textareaEl.value = body;
  renderInsightMeta(runMeta, insight, sectorLabel);
  renderInsightDigestHtml(insight.text || "");
}

async function fetchInsightForGithubRun(ghRunId, sectorQuery) {
  const { proxyBase } = cfg();
  let path = `/insights/github-run/${encodeURIComponent(String(ghRunId))}`;
  const s = String(sectorQuery || "").trim().toLowerCase();
  if (s) path += `?sector=${encodeURIComponent(s)}`;
  const res = await fetch(`${proxyBase}${path}`, {
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(
      typeof data.error === "string" ? data.error : `${res.status} ${res.statusText}`,
    );
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

async function fillRecentTitanRunsSelect(selectEl, { maxRuns = 5 } = {}) {
  if (!selectEl) return;
  const runs = await ghApi("/runs?limit=40");
  const workflowRuns = runs.workflow_runs || [];
  const titanFile = WORKFLOWS.runTitan;
  const titanRuns = workflowRuns.filter((r) => (r.path || "").split("/").pop() === titanFile);
  const pick = titanRuns.slice(0, maxRuns);
  selectEl.innerHTML = "";
  const opt0 = document.createElement("option");
  opt0.value = "";
  opt0.textContent = pick.length ? "— Choose a run —" : "— No Run Titan Now runs in the last page —";
  selectEl.appendChild(opt0);
  for (const r of pick) {
    const o = document.createElement("option");
    o.value = String(r.id);
    const title = (r.display_title || r.name || "").trim().replace(/\s+/g, " ");
    o.textContent = `#${r.run_number} ${r.status}/${r.conclusion || "-"}${title ? ` — ${title.slice(0, 72)}` : ""}`;
    selectEl.appendChild(o);
  }
}

function syncSectorDropdowns(sectorId) {
  const sid = String(sectorId || "").trim().toLowerCase();
  if (!sid || !SECTOR_INPUT_RE.test(sid)) return;
  for (const id of ["sectorId", "insightSectorId"]) {
    const sel = el(id);
    if (!sel) continue;
    const ok = [...sel.options].some((o) => o.value === sid);
    if (ok) sel.value = sid;
  }
}

/**
 * Lists recent workflow runs; if the latest Run Titan Now job completed successfully,
 * fetches workflow inputs and loads the matching Supabase digest into the insight textarea (when present).
 */
async function refreshRunsAndMaybeLoadInsight(insightTextareaId = null) {
  const health = await checkConnection();
  const runs = await ghApi("/runs?limit=40");
  const workflowRuns = runs.workflow_runs || [];
  const titanFile = WORKFLOWS.runTitan;
  const titanRuns = workflowRuns.filter((r) => (r.path || "").split("/").pop() === titanFile);
  const controlRuns = workflowRuns.filter((r) => {
    const name = (r.path || "").split("/").pop();
    return Object.values(WORKFLOWS).includes(name);
  });
  const listSource = controlRuns.length ? controlRuns : workflowRuns;
  const lines = listSource.slice(0, 10).map(
    (r) => `${r.status}/${r.conclusion || "-"} | ${r.name} | #${r.run_number} | ${r.html_url}`,
  );
  const insightTextarea = insightTextareaId ? el(insightTextareaId) : null;

  if (health.has_supabase_insights !== true) {
    setStatus(
      lines.join("\n") +
        "\n\nSupabase insights: not configured on titan-proxy. Add Worker secrets SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (Cloudflare dashboard or `npx wrangler secret put …` from repo root). See docs/PROXY_SETUP.md (Supabase secrets).",
    );
    if (insightTextarea) insightTextarea.value = "";
    return;
  }

  let insightNote = "";

  const latestTitan = titanRuns[0];
  if (!latestTitan) {
    insightNote = "\n\nNo “Run Titan Now” workflow in the recent list.";
  } else if (latestTitan.status !== "completed") {
    insightNote = `\n\nLatest Run Titan: ${latestTitan.status} (#${latestTitan.run_number}). Insight loads when status is completed.`;
    if (insightTextarea) insightTextarea.value = "";
  } else if (latestTitan.conclusion !== "success") {
    insightNote = `\n\nLatest Run Titan finished with conclusion “${latestTitan.conclusion || "unknown"}”. No insight pulled.`;
    if (insightTextarea) insightTextarea.value = "";
  } else {
    const detail = await ghApi(`/workflow-run/${latestTitan.id}`);
    const inputs = detail.inputs || {};
    const mode = String(inputs.mode || "").trim().toLowerCase();
    let sectorToLoad = String(inputs.sector_id || "").trim().toLowerCase();

    if (mode === "sector" || mode === "custom") {
      syncSectorDropdowns(sectorToLoad);
    } else {
      const fromUi = (el("insightSectorId") || el("sectorId"))?.value || "";
      sectorToLoad = String(fromUi).trim().toLowerCase();
    }

    if (mode === "portfolio" || mode === "live") {
      insightNote = `\n\nLatest run mode is “${mode}”. Supabase digest auto-load applies to sector/custom and all_sectors (using the sector you pick).`;
      if (insightTextarea) insightTextarea.value = "";
    } else if (!sectorToLoad || !SECTOR_INPUT_RE.test(sectorToLoad)) {
      insightNote =
        "\n\nPick a valid sector in the Sector ID dropdown, then tap Refresh again to load the latest Supabase digest for that sector.";
      if (insightTextarea) insightTextarea.value = "";
    } else {
      const ins = await fetchInsightTextForSector(sectorToLoad);
      if (ins && insightTextarea) {
        applyInsightToTextarea(insightTextarea, ins, sectorToLoad);
        insightNote = `\n\nSupabase digest loaded for “${sectorToLoad}” (after successful Run Titan #${latestTitan.run_number}, mode=${mode || "?"}).`;
      } else {
        insightNote = `\n\nNo Supabase row yet for “${sectorToLoad}” (or persist disabled). Workflow succeeded — wait for DB or run sql/alter_llm_digest_memory_add_full_digest.sql if needed.`;
        if (insightTextarea) insightTextarea.value = "";
      }
    }
  }
  setStatus(lines.join("\n") + insightNote);
}

function initProxyLine() {
  const proxyEl = el("proxyBase");
  if (proxyEl) {
    proxyEl.textContent = normalizeProxyBase(PROXY_BASE);
  }
}

async function initInsightsPage() {
  let lastWorkflowDetail = null;
  initProxyLine();
  await initSectorOptions("insightSectorId");
  const params = new URLSearchParams(window.location.search || "");
  const qsSector = String(params.get("sector") || "").trim().toLowerCase();
  if (qsSector && SECTOR_INPUT_RE.test(qsSector)) {
    syncSectorDropdowns(qsSector);
  }

  const runSel = el("insightRecentRun");
  const sectorCard = el("insightAllSectorsSectorCard");
  const digestTa = el("insightDigestBody");

  async function refreshRunList() {
    setWorking("Refresh run list");
    await checkConnection();
    await fillRecentTitanRunsSelect(runSel, { maxRuns: 5 });
    lastWorkflowDetail = null;
    if (sectorCard) sectorCard.classList.add("hidden");
    if (digestTa) digestTa.value = "";
    setStatus("Pick a Run Titan job, then tap “Load digest for selected run”.");
  }

  async function onRunSelectionChanged() {
    const rid = String(runSel?.value || "").trim();
    if (!rid) {
      lastWorkflowDetail = null;
      if (sectorCard) sectorCard.classList.add("hidden");
      return;
    }
    try {
      setWorking("Load run metadata");
      lastWorkflowDetail = await ghApi(`/workflow-run/${encodeURIComponent(rid)}`);
      const mode = String(lastWorkflowDetail.inputs?.mode || "").trim().toLowerCase();
      const sid = String(lastWorkflowDetail.inputs?.sector_id || "").trim().toLowerCase();
      if (sid && SECTOR_INPUT_RE.test(sid)) syncSectorDropdowns(sid);
      if (mode === "all_sectors" || (!mode && !sid)) {
        if (sectorCard) sectorCard.classList.remove("hidden");
        setStatus(
          mode === "all_sectors"
            ? `Run #${lastWorkflowDetail.run_number}: all_sectors — pick a sector below, then load digest.`
            : `Run #${lastWorkflowDetail.run_number}: pick a sector below (GitHub did not return workflow inputs), then load digest.`,
        );
      } else {
        if (sectorCard) sectorCard.classList.add("hidden");
        setStatus(
          `Run #${lastWorkflowDetail.run_number}: mode=${mode || "sector"}${sid ? `, sector=${sid}` : ""}. Tap “Load digest for selected run”.`,
        );
      }
    } catch (e) {
      lastWorkflowDetail = null;
      if (sectorCard) sectorCard.classList.add("hidden");
      setStatus(`Could not load run details:\n${e.message}`);
    }
  }

  const listBtn = el("insightRunListRefreshBtn");
  if (listBtn) {
    listBtn.addEventListener("click", async () => {
      try {
        await refreshRunList();
      } catch (e) {
        setStatus(`Refresh run list failed:\n${e.message}`);
      }
    });
  }

  if (runSel) {
    runSel.addEventListener("change", () => {
      void onRunSelectionChanged();
    });
  }

  const loadByRunBtn = el("insightLoadByRunBtn");
  if (loadByRunBtn) {
    loadByRunBtn.addEventListener("click", async () => {
      try {
        const health = await checkConnection();
        if (health.has_supabase_insights !== true) {
          setStatus(
            "Supabase insights are not configured on the proxy. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on titan-proxy.",
          );
          return;
        }
        const rid = String(runSel?.value || "").trim();
        if (!rid) {
          setStatus("Choose one of the last five Run Titan runs first.");
          return;
        }
        if (!lastWorkflowDetail || String(lastWorkflowDetail.id) !== rid) {
          setWorking("Load run metadata");
          lastWorkflowDetail = await ghApi(`/workflow-run/${encodeURIComponent(rid)}`);
        }
        const mode = String(lastWorkflowDetail.inputs?.mode || "").trim().toLowerCase();
        let sectorQ = String(el("insightSectorId")?.value || "").trim().toLowerCase();
        if (!sectorQ) {
          sectorQ = String(lastWorkflowDetail.inputs?.sector_id || "").trim().toLowerCase();
        }
        if (mode === "all_sectors" && (!sectorQ || !SECTOR_INPUT_RE.test(sectorQ))) {
          setStatus("For all_sectors runs, pick a sector in the sector list.");
          return;
        }
        setWorking("Load digest from Supabase");
        let data;
        try {
          data = await fetchInsightForGithubRun(rid, sectorQ);
        } catch (e) {
          const payload = e.payload || {};
          if (
            (payload.code === "sector_required" || payload.code === "bad_sector") &&
            Array.isArray(payload.available_sectors) &&
            payload.available_sectors.length > 0 &&
            sectorCard
          ) {
            sectorCard.classList.remove("hidden");
            setStatus(
              `${e.message}\n\nSaved sectors for this run: ${payload.available_sectors.join(", ")}.\nPick one below and tap “Load digest for selected run” again.`,
            );
            return;
          }
          throw e;
        }
        const ins = data.insight;
        const meta = {
          github_run_number: data.github_run_number,
          github_run_id: data.github_run_id,
          workflow_mode: data.workflow_mode,
        };
        if (!ins || !String(ins.text || "").trim()) {
          if (digestTa) digestTa.value = "";
          clearInsightDigestView();
          setStatus(
            (data.note ||
              "No digest text for this run and sector (or row missing).") +
              (data.workflow_mode === "portfolio" || data.workflow_mode === "live"
                ? ""
                : "\n\nTip: digests are only saved when GitHub Actions runs with TITAN_ENABLE_ANALYSIS_STORE=1 (and Supabase tables exist). Older runs never wrote rows—dispatch a new Run Titan Now after the workflow fix is on the branch you use."),
          );
          return;
        }
        const sectorLabel = data.sector || ins.sector || sectorQ;
        applyInsightToTextarea(digestTa, ins, sectorLabel, meta);
        setStatus(
          `Digest loaded for GitHub run #${data.github_run_number ?? "?"} (${data.workflow_mode || "?"})${data.note ? `\n\n${data.note}` : ""}`,
        );
      } catch (e) {
        if (digestTa) digestTa.value = "";
        clearInsightDigestView();
        setStatus(`Load digest failed:\n${e.message}`);
      }
    });
  }

  const ctrlRefresh = el("insightControlStyleRefreshBtn");
  if (ctrlRefresh) {
    ctrlRefresh.addEventListener("click", async () => {
      try {
        setWorking("Refresh workflow runs (status only)");
        await refreshRunsAndMaybeLoadInsight(null);
      } catch (e) {
        setStatus(`Refresh failed:\n${e.message}`);
      }
    });
  }

  await refreshRunList().catch((e) => {
    setStatus(`Could not load run list:\n${e.message}`);
  });
}

function wireEvents() {
  const breezeLoginBtn = el("breezeLoginBtn");
  if (breezeLoginBtn) {
    breezeLoginBtn.addEventListener("click", () => {
      try {
        const { proxyBase } = cfg();
        window.open(`${proxyBase}/breeze-login`, "_blank", "noopener,noreferrer");
      } catch (e) {
        setStatus(`Breeze login failed:\n${e.message}`);
      }
    });
  }

  const runModeEl = el("runMode");
  if (runModeEl) {
    runModeEl.addEventListener("change", () => {
      setSectorModeUi(runModeEl.value);
    });
  }

  const testConnBtn = el("testConnBtn");
  if (testConnBtn) {
    testConnBtn.addEventListener("click", async () => {
      try {
        setWorking("Test connection");
        await checkConnection({ showSuccess: true });
      } catch (e) {
        setStatus(`Connection test failed:\n${e.message}`);
      }
    });
  }

  const runTitanBtn = el("runTitanBtn");
  if (runTitanBtn) {
    runTitanBtn.addEventListener("click", async () => {
      try {
        setWorking("Validate run inputs");
        const inputs = buildRunTitanInputs();
        const modeLabel = inputs.mode === "custom" ? "custom symbol analysis" : `${inputs.mode} analysis`;
        setWorking(`Dispatch Run Titan (${modeLabel})`);
        await checkConnection();
        let freshnessNote = "";
        try {
          const news = await fetchGlobalNewsStatus();
          if (news && news.fresh !== true) {
            freshnessNote =
              "Global news snapshot is stale; ranking path will attempt refresh and gracefully fallback if feeds fail.";
          }
        } catch (_e) {
          freshnessNote =
            "Could not read global-news freshness; run pipeline will attempt refresh/fallback automatically.";
        }
        let suffix = "";
        if (inputs.mode === "sector") {
          suffix = `When the GitHub Action completes, open **Past runs & insights** and load that run’s digest.`;
        } else if (inputs.mode === "custom") {
          suffix = `When the run completes, open **Past runs & insights** and load that run’s digest.`;
        } else if (inputs.mode === "all_sectors") {
          suffix =
            "When the job completes, open **Past runs & insights**, pick the run, choose a sector, then load digest.";
        }
        if (freshnessNote) {
          suffix = suffix ? `${suffix}\n${freshnessNote}` : freshnessNote;
        }
        await dispatchWorkflow(WORKFLOWS.runTitan, inputs, suffix);
      } catch (e) {
        setStatus(`Run Titan dispatch failed:\n${e.message}`);
      }
    });
  }

  const reconcileScopeEl = el("reconcileScope");
  if (reconcileScopeEl) {
    reconcileScopeEl.addEventListener("change", () => {
      const hint = el("reconcileSectorHint");
      if (hint) {
        hint.textContent =
          reconcileScopeEl.value === "sector"
            ? "Used only when scope=sector."
            : "Ignored for all-stocks scope.";
      }
    });
  }

  const runReconcileBtn = el("runReconcileBtn");
  if (runReconcileBtn) {
    runReconcileBtn.addEventListener("click", async () => {
      try {
        setWorking("Validate reconcile inputs");
        const inputs = buildReconcileInputs();
        setWorking("Dispatch EOD reconcile");
        await checkConnection();
        await dispatchWorkflow(
          WORKFLOWS.runReconcile,
          inputs,
          "Reconcile workflow dispatched on main. Report-only email when data is matured; expect insufficient-data messaging until Titan runs populate Supabase.",
          "main",
        );
      } catch (e) {
        setStatus(`Reconcile dispatch failed:\n${e.message}`);
      }
    });
  }

  const refreshGlobalNewsBtn = el("refreshGlobalNewsBtn");
  if (refreshGlobalNewsBtn) {
    refreshGlobalNewsBtn.addEventListener("click", async () => {
      try {
        setWorking("Refresh global news snapshot");
        const out = await refreshGlobalNewsSnapshot();
        setStatus(
          `Global news refreshed.\nRefreshed at: ${out.refresh.refreshed_at || "n/a"}\n` +
            `Fetched items: ${out.refresh.item_count ?? "?"}\n` +
            `Fresh now: ${out.status.fresh === true ? "yes" : "no"}`,
        );
      } catch (e) {
        setStatus(`Global news refresh failed:\n${e.message}`);
      }
    });
  }

  const fetchSymbolNewsBtn = el("fetchSymbolNewsBtn");
  if (fetchSymbolNewsBtn) {
    fetchSymbolNewsBtn.addEventListener("click", async () => {
      try {
        setWorking("Dispatch symbol news fetch");
        await checkConnection();
        await dispatchWorkflow(
          WORKFLOWS.fetchSymbolNews,
          {},
          "Runs scripts/fetch_news_batch.py (all sectors). Titan runs only read news_feed / symbol_news_snapshots.",
          "news",
        );
      } catch (e) {
        setStatus(`Symbol news fetch dispatch failed:\n${e.message}`);
      }
    });
  }

  const validateBtn = el("validateBtn");
  if (validateBtn) {
    validateBtn.addEventListener("click", async () => {
      try {
        setWorking("Validate token");
        await checkConnection();
        await dispatchWorkflow(WORKFLOWS.validate);
      } catch (e) {
        setStatus(`Validate dispatch failed:\n${e.message}`);
      }
    });
  }

  const persistBtn = el("persistBtn");
  if (persistBtn) {
    persistBtn.addEventListener("click", async () => {
      try {
        setWorking("Persist token");
        const tokenInput = validateBreezeTokenInputForPersist(el("tokenInput")?.value || "");
        await checkConnection();
        await dispatchWorkflow(WORKFLOWS.persist, { breeze_token_input: tokenInput });
      } catch (e) {
        setStatus(`Persist dispatch failed:\n${e.message}`);
      }
    });
  }

  const refreshBtn = el("refreshBtn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      try {
        setWorking("Refresh runs & insight");
        await refreshRunsAndMaybeLoadInsight(null);
      } catch (e) {
        setStatus(`Refresh failed:\n${e.message}`);
      }
    });
  }

  const openInsightsByRunBtn = el("openInsightsByRunBtn");
  if (openInsightsByRunBtn) {
    openInsightsByRunBtn.addEventListener("click", () => {
      window.open("./insights.html", "_blank", "noopener,noreferrer");
    });
  }

  const portfolioScanBtn = el("portfolioScanBtn");
  if (portfolioScanBtn) {
    portfolioScanBtn.addEventListener("click", async () => {
      try {
        setWorking("Parse Portfolio PDF/Text");
        const payload = await buildPortfolioSymbolsPayload();
        setWorking(`Dispatch Portfolio Position Analysis (${payload.parsed_count} holdings)`);
        await checkConnection();
        const inputs = {
          mode: payload.mode,
          custom_exchange: payload.custom_exchange,
          portfolio_holdings_json: payload.portfolio_holdings_json,
          portfolio_max_positions: payload.portfolio_max_positions,
        };
        await dispatchWorkflow(WORKFLOWS.runTitan, inputs);
        const rejectLine =
          payload.rejected_count > 0
            ? `\nRejected first-column entries: ${payload.rejected_count}` +
              (payload.rejected_preview ? `\nRejected sample: ${payload.rejected_preview}` : "")
            : "\nRejected first-column entries: 0";
        setStatus(
          `Dispatched portfolio position analysis.\n` +
            `Parsed holdings (${payload.parsed_count}): ${payload.parsed_symbols_preview}${rejectLine}`,
        );
      } catch (e) {
        setStatus(`Portfolio quick scan failed:\n${e.message}`);
      }
    });
  }
}

async function initStorage() {
  initProxyLine();
  await initSectorOptions("sectorId");
  await initSectorOptions("reconcileSectorId");
  const runModeEl = el("runMode");
  if (runModeEl) {
    setSectorModeUi(runModeEl.value);
  }
  fetchGlobalNewsStatus().catch((e) => {
    const host = el("globalNewsFreshness");
    if (host) {
      host.innerHTML = '<span class="chip chip-error">Status unavailable</span>';
      host.title = String(e.message || e);
    }
  });
}

const UI_PAGE = document.body.getAttribute("data-page") || "control";

try {
  if (UI_PAGE === "insights") {
    initInsightsPage().catch((e) => {
      setStatus(`UI init failed:\n${e.message}`);
    });
  } else {
    wireEvents();
    initStorage().catch((e) => {
      setStatus(`UI init failed:\n${e.message}`);
    });
    setStatus("UI ready.");
  }
} catch (e) {
  setStatus(`UI init failed:\n${e.message}`);
}
