const WORKFLOWS = {
  runTitan: "run_titan_now.yml",
  validate: "validate_breeze_token_manual.yml",
  persist: "persist_breeze_token_manual.yml",
};
const PROXY_BASE = "https://titan-proxy.arunjain-real.workers.dev";
const SECTOR_OPTIONS = [
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
];
const RUN_MODES = new Set(["sector", "all_sectors", "live", "custom"]);
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
  const knownApiSuffixes = new Set(["health", "runs", "dispatch"]);
  while (parts.length && knownApiSuffixes.has(parts[parts.length - 1].toLowerCase())) {
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
  const allSectorWorkersEl = el("allSectorWorkers");
  const customSymbolsEl = el("customSymbols");
  const customSymbolsHintEl = el("customSymbolsHint");
  const customExchangeEl = el("customExchange");
  const customExchangeHintEl = el("customExchangeHint");
  if (!sectorEl) return;
  const isSectorMode = mode === "sector";
  const isAllSectorsMode = mode === "all_sectors";
  const isCustomMode = mode === "custom";
  sectorEl.disabled = !isSectorMode;
  if (allSectorWorkersEl) {
    allSectorWorkersEl.disabled = !isAllSectorsMode;
  }
  if (customSymbolsEl) {
    customSymbolsEl.disabled = !isCustomMode;
  }
  if (customExchangeEl) {
    customExchangeEl.disabled = !isCustomMode;
  }
  if (hintEl) {
    hintEl.textContent = isSectorMode
      ? "Used only when mode=sector."
      : "Ignored for selected mode.";
  }
  if (customSymbolsHintEl) {
    customSymbolsHintEl.textContent = isCustomMode
      ? "Used only when mode=custom."
      : "Ignored for selected mode.";
  }
  if (customExchangeHintEl) {
    customExchangeHintEl.textContent = isCustomMode
      ? "Used only when mode=custom."
      : "Ignored for selected mode.";
  }
}

function initSectorOptions() {
  const sectorEl = el("sectorId");
  if (!sectorEl) return;
  sectorEl.innerHTML = "";
  for (const sid of SECTOR_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = sid;
    opt.textContent = sid;
    if (sid === "defence") opt.selected = true;
    sectorEl.appendChild(opt);
  }
}

function classifyProxyError(status, responseText) {
  const body = String(responseText || "");
  if (status === 404) {
    return (
      "404 from proxy endpoint.\n" +
      "This URL does not expose Titan API routes (/health, /dispatch, /runs).\n" +
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
    const hint = classifyProxyError(res.status, txt);
    const hintBlock = hint ? `\nHint: ${hint}\n` : "\n";
    throw new Error(`${res.status} ${res.statusText}${hintBlock}${txt}`);
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
    setStatus(
      `Connection OK\nProxy repo: ${health.repo}\nPAT configured: ${Boolean(health.has_pat)}\nAllowed workflows: ${flows}`,
    );
  }
  return health;
}

async function dispatchWorkflow(filename, inputs = {}) {
  await ghApi("/dispatch", "POST", { workflow: filename, ref: "main", inputs });
  setStatus(`Dispatched ${filename} successfully.`);
}

function normalizePositiveInt(raw, field, { min = 1, max = 500 } = {}) {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (!/^\d+$/.test(value)) {
    throw new Error(`${field} must be a whole number.`);
  }
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new Error(`${field} must be between ${min} and ${max}.`);
  }
  return String(n);
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
      if (EXCHANGE_OPTIONS.has(leftNorm) && CUSTOM_SYMBOL_TOKEN_RE.test(rightNorm)) {
        return { symbol: rightNorm, exchange: leftNorm };
      }
      if (EXCHANGE_OPTIONS.has(rightNorm) && CUSTOM_SYMBOL_TOKEN_RE.test(leftNorm)) {
        return { symbol: leftNorm, exchange: rightNorm };
      }
    }
  }
  const sym = normalizeHoldingSymbol(raw);
  if (!CUSTOM_SYMBOL_TOKEN_RE.test(sym)) return null;
  return { symbol: sym, exchange: defaultExchange };
}

function parseHoldingsSymbolsFromText(rawText, defaultExchange = "NSE") {
  const lines = String(rawText || "").split(/\r?\n/);
  const found = [];
  const seen = new Set();
  for (const line of lines) {
    const cleaned = String(line || "").trim();
    if (!cleaned || cleaned.startsWith("#")) continue;
    let tokens = cleaned.split(/[,\t|;]+/).map((t) => t.trim()).filter(Boolean);
    if (tokens.length === 1) {
      tokens = cleaned.split(/\s+/).map((t) => t.trim()).filter(Boolean);
    }
    if (tokens.length < 2) continue;
    let qtyIdx = -1;
    for (let i = 0; i < tokens.length; i += 1) {
      const tok = tokens[i].replace(/,/g, "");
      if (/^[+-]?\d+(?:\.\d+)?$/.test(tok)) {
        qtyIdx = i;
        break;
      }
    }
    if (qtyIdx < 1) continue;
    for (let j = qtyIdx - 1; j >= 0; j -= 1) {
      const parsed = parseSymbolWithExchange(tokens[j], defaultExchange);
      if (!parsed) continue;
      if (COMMON_NON_SYMBOLS.has(parsed.symbol)) continue;
      if (!seen.has(parsed.symbol)) {
        seen.add(parsed.symbol);
        found.push(parsed.symbol);
      }
      break;
    }
  }
  return found.slice(0, MAX_CUSTOM_SYMBOLS);
}

async function extractPdfText(file) {
  const pdfjsLib = await import("https://cdn.jsdelivr.net/npm/pdfjs-dist@4.5.136/build/pdf.mjs");
  if (pdfjsLib && pdfjsLib.GlobalWorkerOptions) {
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.5.136/build/pdf.worker.mjs";
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  const pdf = await pdfjsLib.getDocument({ data: bytes }).promise;
  const pageTexts = [];
  for (let i = 1; i <= pdf.numPages; i += 1) {
    const page = await pdf.getPage(i);
    const tc = await page.getTextContent();
    const txt = (tc.items || []).map((x) => String(x.str || "")).join(" ");
    pageTexts.push(txt);
  }
  return pageTexts.join("\n");
}

async function buildPortfolioSymbolsPayload() {
  const exchange = String(el("portfolioExchange")?.value || "NSE")
    .trim()
    .toUpperCase();
  if (!EXCHANGE_OPTIONS.has(exchange)) {
    throw new Error("Portfolio exchange must be NSE or BSE.");
  }
  const sectorId = String(el("portfolioSectorId")?.value || "portfolio_pdf")
    .trim()
    .toLowerCase();
  if (!/^[a-z0-9_]{1,64}$/.test(sectorId)) {
    throw new Error("Portfolio sector label must match [a-z0-9_]{1,64}.");
  }
  const maxSymbols = normalizePositiveInt(el("portfolioMaxSymbols")?.value, "Portfolio max symbols", {
    min: 1,
    max: 120,
  });
  const pasted = String(el("portfolioHoldingsText")?.value || "");
  const pdfFile = el("portfolioPdfFile")?.files?.[0] || null;
  let combinedText = pasted;
  if (pdfFile) {
    try {
      const pdfText = await extractPdfText(pdfFile);
      combinedText = [pdfText, pasted].filter(Boolean).join("\n");
    } catch (e) {
      throw new Error(`PDF extraction failed. ${e.message || e}`);
    }
  }
  const symbols = parseHoldingsSymbolsFromText(combinedText, exchange);
  if (!symbols.length) {
    throw new Error("No symbols parsed from PDF/text. Use lines like NSE:RELIANCE, 10");
  }
  return {
    mode: "custom",
    sector_id: sectorId,
    custom_exchange: exchange,
    custom_symbols: symbols.join(","),
    max_symbols: maxSymbols || String(symbols.length),
    workers: normalizePositiveInt(el("workers")?.value, "Workers", { min: 1, max: 16 }) || "4",
    all_sector_workers: "",
    parsed_count: symbols.length,
  };
}

function buildRunTitanInputs() {
  const mode = (el("runMode")?.value || "sector").trim();
  if (!RUN_MODES.has(mode)) {
    throw new Error("Mode is invalid.");
  }
  const workers = normalizePositiveInt(el("workers")?.value, "Workers", { min: 1, max: 16 });
  const maxSymbols = normalizePositiveInt(el("maxSymbols")?.value, "Max Symbols", {
    min: 1,
    max: 500,
  });
  const allSectorWorkers = normalizePositiveInt(
    el("allSectorWorkers")?.value,
    "All-sector workers",
    { min: 1, max: 200 },
  );
  const sectorId = (el("sectorId")?.value || "").trim();

  const inputs = {
    mode,
    sector_id: sectorId,
    max_symbols: maxSymbols,
    workers,
    all_sector_workers: allSectorWorkers,
  };

  if (mode === "sector" && !sectorId) {
    throw new Error("Sector ID is required for sector mode.");
  }

  if (mode === "custom") {
    const parsed = parseCustomSymbols(el("customSymbols")?.value || "");
    const customExchange = String(el("customExchange")?.value || "NSE")
      .trim()
      .toUpperCase();
    if (!EXCHANGE_OPTIONS.has(customExchange)) {
      throw new Error("Custom exchange must be NSE or BSE.");
    }
    const customSectorId = sectorId || "custom_ui";
    inputs.sector_id = customSectorId;
    inputs.custom_symbols = parsed.join(",");
    inputs.custom_exchange = customExchange;
  }

  return inputs;
}

async function loadLatestRuns() {
  const runs = await ghApi("/runs?limit=20");
  const relevant = (runs.workflow_runs || []).filter((r) =>
    Object.values(WORKFLOWS).includes(r.path.split("/").pop()),
  );
  if (!relevant.length) {
    setStatus("No recent runs for control workflows.");
    return;
  }
  const lines = relevant.slice(0, 10).map(
    (r) => `${r.status}/${r.conclusion || "-"} | ${r.name} | #${r.run_number} | ${r.html_url}`,
  );
  setStatus(lines.join("\n"));
}

function wireEvents() {
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
        setWorking("Test Connection");
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
        await dispatchWorkflow(WORKFLOWS.runTitan, inputs);
      } catch (e) {
        setStatus(`Run Titan dispatch failed:\n${e.message}`);
      }
    });
  }

  const validateBtn = el("validateBtn");
  if (validateBtn) {
    validateBtn.addEventListener("click", async () => {
      try {
        setWorking("Dispatch Validate Token");
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
        setWorking("Dispatch Persist Token");
        const tokenInput = (el("tokenInput")?.value || "").trim();
        if (!tokenInput) throw new Error("Token input is required.");
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
        setWorking("Refresh Status");
        await loadLatestRuns();
      } catch (e) {
        setStatus(`Refresh failed:\n${e.message}`);
      }
    });
  }

  const portfolioScanBtn = el("portfolioScanBtn");
  if (portfolioScanBtn) {
    portfolioScanBtn.addEventListener("click", async () => {
      try {
        setWorking("Parse Portfolio PDF/Text");
        const payload = await buildPortfolioSymbolsPayload();
        setWorking(`Dispatch Portfolio Quick Scan (${payload.parsed_count} symbols)`);
        await checkConnection();
        const inputs = {
          mode: payload.mode,
          sector_id: payload.sector_id,
          custom_exchange: payload.custom_exchange,
          custom_symbols: payload.custom_symbols,
          max_symbols: payload.max_symbols,
          workers: payload.workers,
          all_sector_workers: payload.all_sector_workers,
        };
        await dispatchWorkflow(WORKFLOWS.runTitan, inputs);
      } catch (e) {
        setStatus(`Portfolio quick scan failed:\n${e.message}`);
      }
    });
  }
}

function initStorage() {
  initSectorOptions();
  const proxyEl = el("proxyBase");
  if (proxyEl) {
    proxyEl.textContent = normalizeProxyBase(PROXY_BASE);
  }
  const runModeEl = el("runMode");
  if (runModeEl) {
    setSectorModeUi(runModeEl.value);
  }
}

try {
  wireEvents();
  initStorage();
  setStatus("UI ready. Enter token and tap an action button.");
} catch (e) {
  setStatus(`UI init failed:\n${e.message}`);
}
