const WORKFLOWS = {
  runTitan: "run_titan_now.yml",
  validate: "validate_breeze_token_manual.yml",
  persist: "persist_breeze_token_manual.yml",
};

const el = (id) => document.getElementById(id);
const statusEl = el("status");

function setStatus(msg) {
  statusEl.textContent = msg;
}

function setWorking(label) {
  setStatus(`Working: ${label} ...`);
}

function cfg() {
  const proxyBase = el("proxyBase").value.trim().replace(/\/+$/, "");
  if (!proxyBase) throw new Error("Proxy URL is required.");
  try {
    localStorage.setItem("titan_control_proxy_base", proxyBase);
  } catch (_e) {
    // Ignore storage failures.
  }
  return { proxyBase };
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
    throw new Error(`${res.status} ${res.statusText}\n${txt}`);
  }
  if (res.status === 204) return null;
  return await res.json();
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
  el("runTitanBtn").addEventListener("click", async () => {
    try {
      setWorking("Dispatch Run Titan");
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
  try {
    const saved = localStorage.getItem("titan_control_proxy_base");
    if (saved) {
      el("proxyBase").value = saved;
    }
  } catch (_e) {
    // Storage may be disabled in private mode; ignore.
  }
}

try {
  wireEvents();
  initStorage();
  setStatus("UI ready. Enter token and tap an action button.");
} catch (e) {
  setStatus(`UI init failed:\n${e.message}`);
}
