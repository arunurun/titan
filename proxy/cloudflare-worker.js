/**
 * Tokenless dispatch proxy for Titan Mobile Control UI.
 *
 * Deploy as Cloudflare Worker and set secrets:
 * - GITHUB_PAT
 * - REPO_OWNER (e.g., arunurun)
 * - REPO_NAME (e.g., titan)
 */

const ALLOWED_WORKFLOWS = new Set([
  "run_titan_now.yml",
  "validate_breeze_token_manual.yml",
  "persist_breeze_token_manual.yml",
]);
const RUN_MODES = new Set(["sector", "all_sectors", "live", "custom"]);
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

  const cleaned = {
    mode,
    sector_id: "",
    max_symbols: maxSymbols,
    workers,
    all_sector_workers: allSectorWorkers,
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
  } else {
    cleaned.sector_id = "";
  }

  return cleaned;
}

function sanitizeDispatchPayload(workflow, inputs) {
  if (workflow === "run_titan_now.yml") {
    return sanitizeRunTitanInputs(inputs);
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

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return json({ ok: true }, 204);

    try {
      const url = new URL(request.url);

      if (request.method === "POST" && url.pathname === "/dispatch") {
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

      if (request.method === "GET" && url.pathname === "/runs") {
        const limit = Number(url.searchParams.get("limit") || "20");
        const data = await gh(env, `/actions/runs?per_page=${Math.max(1, Math.min(limit, 100))}`);
        return json(data);
      }

      if (request.method === "GET" && url.pathname === "/health") {
        return json({
          ok: true,
          repo: `${env.REPO_OWNER || "<missing>"}/${env.REPO_NAME || "<missing>"}`,
          has_pat: Boolean(env.GITHUB_PAT),
          allowed_workflows: Array.from(ALLOWED_WORKFLOWS),
        });
      }

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500);
    }
  },
};
