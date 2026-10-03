import { PAGE_HTML } from "./page";

export interface KV {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, options?: { expirationTtl?: number }): Promise<void>;
  delete(key: string): Promise<void>;
}

export interface RateLimit {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

export interface Env {
  CODES: KV;
  RATE_LIMITER?: RateLimit;
}

export const TTL_SECONDS = 600;
const CODE_RE = /^\d{8}$/;
const RANGE = 100_000_000;
const UNIFORM_LIMIT = Math.floor(0x1_0000_0000 / RANGE) * RANGE;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function randomCode(): string {
  const buf = new Uint32Array(1);
  do {
    crypto.getRandomValues(buf);
  } while (buf[0] >= UNIFORM_LIMIT);
  return String(buf[0] % RANGE).padStart(8, "0");
}

export function validServerUrl(raw: unknown): string | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 2048) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password) return null;
  return url.origin + url.pathname.replace(/\/+$/, "");
}

async function createCode(request: Request, env: Env): Promise<Response> {
  let body: { url?: unknown; code?: unknown };
  try {
    body = (await request.json()) as { url?: unknown; code?: unknown };
  } catch {
    return json({ error: "bad_request", message: "invalid JSON" }, 400);
  }
  const url = validServerUrl(body?.url);
  if (!url) return json({ error: "invalid_url", message: "the URL must be https://" }, 400);

  let code = "";
  if (body.code !== undefined && body.code !== null) {
    if (typeof body.code !== "string" || !CODE_RE.test(body.code)) return json({ error: "invalid_code" }, 400);
    if (await env.CODES.get(`code:${body.code}`)) return json({ error: "conflict" }, 409);
    code = body.code;
  } else {
    for (let i = 0; i < 5 && !code; i++) {
      const candidate = randomCode();
      if (!(await env.CODES.get(`code:${candidate}`))) code = candidate;
    }
    if (!code) return json({ error: "unavailable" }, 503);
  }
  const expiresAt = Math.floor(Date.now() / 1000) + TTL_SECONDS;
  await env.CODES.put(`code:${code}`, JSON.stringify({ url, expires_at: expiresAt }), { expirationTtl: TTL_SECONDS });
  return json({ code, expires_at: expiresAt }, 201);
}

async function resolveCode(raw: string, env: Env): Promise<Response> {
  // Decode before stripping separators: without this, "1234%205678" would become "1234205678".
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return json({ error: "not_found" }, 404);
  }
  const code = decoded.replace(/\D/g, "");
  if (!CODE_RE.test(code)) return json({ error: "not_found" }, 404);
  const stored = await env.CODES.get(`code:${code}`);
  if (!stored) return json({ error: "not_found" }, 404);
  await env.CODES.delete(`code:${code}`);
  const entry = JSON.parse(stored) as { url: string; expires_at: number };
  if (entry.expires_at < Date.now() / 1000) return json({ error: "not_found" }, 404);
  return json({ url: entry.url });
}

export async function handle(request: Request, env: Env): Promise<Response> {
  const { pathname } = new URL(request.url);
  if (env.RATE_LIMITER && pathname.startsWith("/v1/")) {
    const key = request.headers.get("cf-connecting-ip") ?? "local";
    const { success } = await env.RATE_LIMITER.limit({ key });
    if (!success) return json({ error: "rate_limited" }, 429);
  }
  if (request.method === "GET" && pathname === "/") {
    return new Response(PAGE_HTML, { headers: { "content-type": "text/html; charset=utf-8" } });
  }
  if (request.method === "POST" && pathname === "/v1/codes") return createCode(request, env);
  const match = pathname.match(/^\/v1\/resolve\/([^/]+)$/);
  if (request.method === "GET" && match) return resolveCode(match[1], env);
  return json({ error: "not_found" }, 404);
}
