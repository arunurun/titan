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

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
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
        const inputs = body.inputs && typeof body.inputs === "object" ? body.inputs : {};

        if (!ALLOWED_WORKFLOWS.has(workflow)) {
          return json({ error: "workflow not allowed" }, 400);
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

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500);
    }
  },
};
