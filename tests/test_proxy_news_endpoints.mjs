import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function loadWorkerModule() {
  const src = await readFile(new URL("../proxy/cloudflare-worker.js", import.meta.url), "utf8");
  const dataUrl = `data:text/javascript;base64,${Buffer.from(src, "utf8").toString("base64")}`;
  return import(dataUrl);
}

function makeEnv(overrides = {}) {
  return {
    SUPABASE_URL: "https://mock.supabase.co",
    SUPABASE_SERVICE_ROLE_KEY: "service-key",
    TITAN_NEWS_FEEDS: "https://feed-a.local/rss",
    ...overrides,
  };
}

test("GET /news/status returns freshness payload", async () => {
  const mod = await loadWorkerModule();
  const worker = mod.default;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.includes("/rest/v1/global_news_snapshots?select=")) {
      return new Response(
        JSON.stringify([
          {
            refreshed_at: "2026-01-03T00:00:00Z",
            item_count: 4,
            fetch_status: "ok",
            refresh_error: "",
          },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    throw new Error(`unexpected fetch in status test: ${u}`);
  };
  try {
    const res = await worker.fetch(new Request("https://example.com/news/status"), makeEnv());
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ok, true);
    assert.equal(body.snapshot.item_count, 4);
    assert.ok(typeof body.fresh === "boolean");
    assert.ok(body.ttl_hours > 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("POST /news/refresh persists and reports status", async () => {
  const mod = await loadWorkerModule();
  const worker = mod.default;
  const originalFetch = globalThis.fetch;
  let insertCalled = false;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const method = String(init.method || "GET").toUpperCase();
    if (u === "https://feed-a.local/rss") {
      const recent = new Date(Date.now() - 30 * 60 * 1000).toUTCString();
      const xml = `<rss><channel><title>FeedA</title>
      <item><title>AI chip demand surges</title><link>https://x/a1</link>
      <pubDate>${recent}</pubDate><description>Cloud capex growth</description></item>
      </channel></rss>`;
      return new Response(xml, { status: 200 });
    }
    if (u.endsWith("/rest/v1/global_news_snapshots") && method === "POST") {
      insertCalled = true;
      return new Response("", { status: 201 });
    }
    if (u.includes("/rest/v1/global_news_snapshots?select=") && method === "GET") {
      return new Response(
        JSON.stringify([
          {
            refreshed_at: "2026-01-03T00:05:00Z",
            item_count: 1,
            fetch_status: "ok",
            refresh_error: "",
          },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    throw new Error(`unexpected fetch in refresh test: ${u}`);
  };
  try {
    const res = await worker.fetch(
      new Request("https://example.com/news/refresh", { method: "POST" }),
      makeEnv(),
    );
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ok, true);
    assert.equal(insertCalled, true);
    assert.equal(body.item_count, 1);
    assert.ok(typeof body.fresh === "boolean");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
