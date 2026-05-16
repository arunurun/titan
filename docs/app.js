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
  const isSectorMode = mode === "sector";
  sectorEl.disabled = !isSectorMode;
  if (hintEl) {
    hintEl.textContent = isSectorMode
      ? "Used only when mode=sector."
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
  el("runMode").addEventListener("change", () => {
    setSectorModeUi(el("runMode").value);
  });

  el("testConnBtn").addEventListener("click", async () => {
    try {
      setWorking("Test Connection");
      await checkConnection({ showSuccess: true });
    } catch (e) {
      setStatus(`Connection test failed:\n${e.message}`);
    }
  });

  el("runTitanBtn").addEventListener("click", async () => {
    try {
      setWorking("Dispatch Run Titan");
      await checkConnection();
      await dispatchWorkflow(WORKFLOWS.runTitan, {
        mode: el("runMode").value,
        sector_id: el("sectorId").value.trim(),
        max_symbols: el("maxSymbols").value.trim(),
        workers: el("workers").value.trim(),
      });
    } catch (e) {
      setStatus(`Run Titan dispatch failed:\n${e.message}`);
    }
  });

  el("validateBtn").addEventListener("click", async () => {
    try {
      setWorking("Dispatch Validate Token");
      await checkConnection();
      await dispatchWorkflow(WORKFLOWS.validate);
    } catch (e) {
      setStatus(`Validate dispatch failed:\n${e.message}`);
    }
  });

  el("persistBtn").addEventListener("click", async () => {
    try {
      setWorking("Dispatch Persist Token");
      const tokenInput = el("tokenInput").value.trim();
      if (!tokenInput) throw new Error("Token input is required.");
      await checkConnection();
      await dispatchWorkflow(WORKFLOWS.persist, { breeze_token_input: tokenInput });
    } catch (e) {
      setStatus(`Persist dispatch failed:\n${e.message}`);
    }
  });

  el("refreshBtn").addEventListener("click", async () => {
    try {
      setWorking("Refresh Status");
      await loadLatestRuns();
    } catch (e) {
      setStatus(`Refresh failed:\n${e.message}`);
    }
  });
}

function initStorage() {
  initSectorOptions();
  const proxyEl = el("proxyBase");
  if (proxyEl) {
    proxyEl.textContent = normalizeProxyBase(PROXY_BASE);
  }
  setSectorModeUi(el("runMode").value);
}

try {
  wireEvents();
  initStorage();
  setStatus("UI ready. Enter token and tap an action button.");
} catch (e) {
  setStatus(`UI init failed:\n${e.message}`);
}
