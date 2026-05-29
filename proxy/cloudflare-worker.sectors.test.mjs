import test from "node:test";
import assert from "node:assert/strict";

import worker from "./cloudflare-worker.js";

const ORIGINAL_FETCH = globalThis.fetch;

function makeRequest(path) {
  return new Request(`https://example.com${path}`);
}

function withMockedFetch(mockImpl) {
  globalThis.fetch = mockImpl;
  return () => {
    globalThis.fetch = ORIGINAL_FETCH;
  };
}

test("GET /sectors/active returns filtered active sectors", async () => {
  const restoreFetch = withMockedFetch(async (url) => {
    assert.match(String(url), /\/rest\/v1\/sector_catalog/);
    return new Response(
      JSON.stringify([
        { sector_key: "defence", is_active: true },
        { sector_key: "UNKNOWN", is_active: true },
        { sector_key: "non_equity", is_active: true },
        { sector_key: "it", is_active: true },
        { sector_key: "bad-key", is_active: true },
        { sector_key: "it", is_active: true },
      ]),
      { status: 200, headers: { "content-type": "application/json; charset=utf-8" } },
    );
  });
  try {
    const res = await worker.fetch(
      makeRequest("/sectors/active"),
      {
        SUPABASE_URL: "https://demo.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY: "demo-secret",
      },
      {},
    );
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ok, true);
    assert.deepEqual(body.sectors, ["defence", "it"]);
    assert.equal(body.count, 2);
  } finally {
    restoreFetch();
  }
});

test("GET /sectors/active returns 503 when Supabase secrets missing", async () => {
  const restoreFetch = withMockedFetch(async () => {
    throw new Error("fetch should not be called when secrets are missing");
  });
  try {
    const res = await worker.fetch(makeRequest("/sectors/active"), {}, {});
    assert.equal(res.status, 503);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.equal(body.code, "missing_supabase_secrets");
  } finally {
    restoreFetch();
  }
});

test("GET /sectors/active returns 502 for invalid Supabase payload", async () => {
  const restoreFetch = withMockedFetch(async () => {
    return new Response("not-json", { status: 200 });
  });
  try {
    const res = await worker.fetch(
      makeRequest("/sectors/active"),
      {
        SUPABASE_URL: "https://demo.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY: "demo-secret",
      },
      {},
    );
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.ok, false);
    assert.equal(body.code, "sectors_fetch_failed");
  } finally {
    restoreFetch();
  }
});
