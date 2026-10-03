import { beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";
import { TTL_SECONDS, randomCode, validServerUrl, type Env, type KV } from "../src/core";

class MemoryKV implements KV {
  store = new Map<string, { value: string; ttl?: number }>();
  async get(key: string) {
    return this.store.get(key)?.value ?? null;
  }
  async put(key: string, value: string, options?: { expirationTtl?: number }) {
    this.store.set(key, { value, ttl: options?.expirationTtl });
  }
  async delete(key: string) {
    this.store.delete(key);
  }
}

let kv: MemoryKV;
let env: Env;

beforeEach(() => {
  kv = new MemoryKV();
  env = { CODES: kv };
});

const call = (path: string, init?: RequestInit) => worker.fetch(new Request(`https://dir.test${path}`, init), env);
const register = (body: unknown) =>
  call("/v1/codes", { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });

describe("POST /v1/codes", () => {
  it("registers the server code with a TTL and resolves it only once", async () => {
    const r = await register({ url: "https://wc.example.test/", code: "12345678" });
    expect(r.status).toBe(201);
    const body = (await r.json()) as { code: string; expires_at: number };
    expect(body.code).toBe("12345678");
    expect(kv.store.get("code:12345678")?.ttl).toBe(TTL_SECONDS);
    const first = await call("/v1/resolve/12345678");
    expect(first.status).toBe(200);
    expect(await first.json()).toEqual({ url: "https://wc.example.test" });
    expect((await call("/v1/resolve/12345678")).status).toBe(404);
  });

  it("generates a code when the owner only provides the URL (flow B)", async () => {
    const r = await register({ url: "https://wc.example.test" });
    expect(r.status).toBe(201);
    const { code } = (await r.json()) as { code: string };
    expect(code).toMatch(/^\d{8}$/);
  });

  it("rejects a code that is already active", async () => {
    await register({ url: "https://a.test", code: "12345678" });
    const r = await register({ url: "https://b.test", code: "12345678" });
    expect(r.status).toBe(409);
  });

  it.each(["http://wc.test", "https://u:p@wc.test", "javascript:alert(1)", 42, ""])("rejects invalid URL %s", async (url) => {
    const r = await register({ url });
    expect(r.status).toBe(400);
    expect(((await r.json()) as { error: string }).error).toBe("invalid_url");
  });

  it("rejects a malformed code and invalid JSON", async () => {
    expect((await register({ url: "https://wc.test", code: "1234" })).status).toBe(400);
    const r = await call("/v1/codes", { method: "POST", body: "{", headers: { "content-type": "application/json" } });
    expect(r.status).toBe(400);
  });
});

describe("GET /v1/resolve", () => {
  it("ignores typed separators", async () => {
    await register({ url: "https://wc.test", code: "12345678" });
    expect((await call("/v1/resolve/1234-5678")).status).toBe(200);
  });

  it("decodes the path before stripping separators (%20 does not become a digit)", async () => {
    await register({ url: "https://wc.test", code: "12345678" });
    const r = await call("/v1/resolve/1234%205678");
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ url: "https://wc.test" });
  });

  it("returns 404 for a malformed percent escape", async () => {
    await register({ url: "https://wc.test", code: "12345678" });
    expect((await call("/v1/resolve/12345678%E0%A4%A")).status).toBe(404);
    expect(kv.store.has("code:12345678")).toBe(true);
  });

  it("does not return an expired entry even if the KV still has it", async () => {
    await kv.put("code:87654321", JSON.stringify({ url: "https://wc.test", expires_at: 1 }));
    expect((await call("/v1/resolve/87654321")).status).toBe(404);
  });
});

describe("other", () => {
  it("serves the flow B page", async () => {
    const r = await call("/");
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toContain("text/html");
    expect(await r.text()).toContain("wristcall");
  });

  it("applies the rate limit only to the API", async () => {
    env.RATE_LIMITER = { limit: async () => ({ success: false }) };
    expect((await register({ url: "https://wc.test" })).status).toBe(429);
    expect((await call("/")).status).toBe(200);
  });

  it("returns 404 for unknown routes", async () => {
    expect((await call("/v2/x")).status).toBe(404);
  });

  it("generates 8 digit codes and normalizes URLs", () => {
    for (let i = 0; i < 1000; i++) expect(randomCode()).toMatch(/^\d{8}$/);
    expect(validServerUrl("https://x.test/wc/")).toBe("https://x.test/wc");
  });
});
