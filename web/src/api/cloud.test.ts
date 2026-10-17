import { describe, expect, test, vi } from "vitest"
import { ApiError, TimeoutError } from "../lib/errors"
import { mockFetch } from "../test/mockFetch"
import { type CloudAgent, type CloudServer, CloudClient } from "./cloud"

const BASE = "https://cloud.example"
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
const server = (patch: Partial<CloudServer> = {}): CloudServer => ({
  id: "s1", name: "home", url: "https://home.example", kind: "self-hosted", linked: false, agents: [], created_at: "t", updated_at: "t", ...patch,
})
const auth = (init: RequestInit) => (init.headers as Record<string, string>).Authorization

describe("CloudClient", () => {
  test("config: no token, trailing slash on the base is dropped", async () => {
    const f = mockFetch(() => reply({ issuer: "https://id.example", clients: { pwa: "p" }, scopes: [], server_tokens: true }))
    const config = await new CloudClient(`${BASE}/`, f).config()
    expect(f.lastUrl()).toBe(`${BASE}/v1/config`)
    expect(auth(f.lastInit())).toBeUndefined()
    expect(config.clients.pwa).toBe("p")
  })

  test("account and servers carry the access token", async () => {
    const f = mockFetch((url) => (url.endsWith("/v1/account") ? reply({ account: "a", created_at: "t", servers: 1 }) : reply({ servers: [server()] })))
    const cloud = new CloudClient(BASE, f)
    expect((await cloud.account("at")).account).toBe("a")
    expect(auth(f.lastInit())).toBe("Bearer at")
    expect(await cloud.servers("at")).toEqual([server()])
    expect(f.lastUrl()).toBe(`${BASE}/v1/servers`)
  })

  test("cloudDeleteAccount503IsApiError", async () => {
    const f = mockFetch(() => reply({ error: "account_unavailable", message: "The identity provider is unavailable." }, 503))
    const error = await new CloudClient(BASE, f).deleteAccount("at").catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 503, code: "account_unavailable" })
    expect(f.lastInit().method).toBe("DELETE")
  })

  test("deleteAccount: 204 is done", async () => {
    const f = mockFetch(() => new Response(null, { status: 204 }))
    await expect(new CloudClient(BASE, f).deleteAccount("at")).resolves.toBeUndefined()
    expect(f.lastUrl()).toBe(`${BASE}/v1/account`)
  })

  test("addServer: sends the canonical URL", async () => {
    const f = mockFetch(() => reply(server(), 201))
    await new CloudClient(BASE, f).addServer("at", { name: "home", url: "HTTPS://Home.Example:443/", linked: true })
    expect(f.lastInit().method).toBe("POST")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ name: "home", url: "https://home.example", linked: true })
  })

  test("addServer: 409 gives the existing entry", async () => {
    const f = mockFetch((_url, init) =>
      init.method === "POST" ? reply({ error: "conflict", message: "exists" }, 409) : reply({ servers: [server({ id: "other", url: "https://x.example" }), server()] }),
    )
    expect((await new CloudClient(BASE, f).addServer("at", { name: "n", url: "https://home.example/" })).id).toBe("s1")
  })

  test("addServer: 409 without a match stays an error; other errors pass through", async () => {
    const conflict = mockFetch((_url, init) => (init.method === "POST" ? reply({ error: "conflict", message: "exists" }, 409) : reply({ servers: [] })))
    await expect(new CloudClient(BASE, conflict).addServer("at", { name: "n", url: "https://home.example" })).rejects.toMatchObject({ status: 409 })
    const limit = mockFetch(() => reply({ error: "limit", message: "too many" }, 403))
    await expect(new CloudClient(BASE, limit).addServer("at", { name: "n", url: "https://home.example" })).rejects.toMatchObject({ status: 403 })
  })

  test("updateServer, removeServer and putAgents use one escaped path segment", async () => {
    const f = mockFetch((_url, init) => (init.method === "PATCH" ? reply(server({ linked: true })) : init.method === "PUT" ? reply({ agents: [] }) : new Response(null, { status: 204 })))
    const cloud = new CloudClient(BASE, f)
    expect((await cloud.updateServer("at", "s/1", { linked: true })).linked).toBe(true)
    expect(f.lastUrl()).toBe(`${BASE}/v1/servers/s%2F1`)
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ linked: true })
    const agents: CloudAgent[] = [{ id: "a", slug: "a", display_name: "A", icon: "x", call_type: "conversation" }]
    await cloud.putAgents("at", "s1", agents)
    expect(f.lastUrl()).toBe(`${BASE}/v1/servers/s1/agents`)
    expect(f.lastInit().method).toBe("PUT")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ agents })
    await cloud.removeServer("at", "s1")
    expect(f.lastInit().method).toBe("DELETE")
    await expect(cloud.updateServer("at", "..", {})).rejects.toThrow("Invalid identifier.")
  })

  test("removeServer: 404 counts as done", async () => {
    const f = mockFetch(() => reply({ error: "not_found", message: "no" }, 404))
    await expect(new CloudClient(BASE, f).removeServer("at", "s1")).resolves.toBeUndefined()
    const gone = mockFetch(() => reply({ error: "boom", message: "x" }, 500))
    await expect(new CloudClient(BASE, gone).removeServer("at", "s1")).rejects.toMatchObject({ status: 500 })
  })

  test("cloudServerTokenSendsCanonicalAudience", async () => {
    const f = mockFetch(() => reply({ token: "t", audience: "https://home.example", expires_at: 5 }))
    const token = await new CloudClient(BASE, f).serverToken("at", "https://Home.Example:443/")
    expect(f.lastUrl()).toBe(`${BASE}/v1/server-tokens`)
    expect(f.lastInit().method).toBe("POST")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ audience: "https://home.example" })
    expect(auth(f.lastInit())).toBe("Bearer at")
    expect(token.token).toBe("t")
  })

  const session = { account: "acc_1", device: "dev_1", access_token: "wc-at", refresh_token: "wcr_x", expires_at: 99 }

  test("createWebSession sends name and web platform without auth", async () => {
    const start = { id: "ws_0123456789abcdef", approve_key: "ak", poll_key: "wcp_x", expires_at: 300 }
    const f = mockFetch(() => reply(start, 201))
    expect(await new CloudClient(BASE, f).createWebSession("Chrome on macOS")).toEqual(start)
    expect(f.lastUrl()).toBe(`${BASE}/v1/web-sessions`)
    expect(f.lastInit().method).toBe("POST")
    expect(f.lastInit().body).toBe('{"device":{"name":"Chrome on macOS","platform":"web"}}')
    expect(auth(f.lastInit())).toBeUndefined()
  })

  test("pollWebSession sends the poll key", async () => {
    const answers = [{ status: "pending", expires_at: 300 }, { status: "approved", ...session }]
    const f = mockFetch(() => reply(answers.shift()))
    const cloud = new CloudClient(BASE, f)
    expect(await cloud.pollWebSession("ws_a", "wcp_x")).toEqual({ status: "pending", expires_at: 300 })
    expect(f.lastUrl()).toBe(`${BASE}/v1/web-sessions/ws_a`)
    expect(f.lastInit().method).toBe("GET")
    expect(auth(f.lastInit())).toBe("Bearer wcp_x")
    expect(await cloud.pollWebSession("ws_a", "wcp_x")).toEqual({ status: "approved", ...session })
  })

  test("pollWebSession 404 is ApiError 404", async () => {
    const f = mockFetch(() => reply({ error: "not_found", message: "web session not found" }, 404))
    const e = await new CloudClient(BASE, f).pollWebSession("ws_a", "wcp_x").catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(404)
  })

  test("exchangeSignIn posts the web device with the Zitadel token", async () => {
    const f = mockFetch(() => reply({ ...session, key_set: true }, 201))
    expect(await new CloudClient(BASE, f).exchangeSignIn("zt", "Safari on iPhone")).toEqual({ ...session, key_set: true })
    expect(f.lastUrl()).toBe(`${BASE}/v1/devices`)
    expect(f.lastInit().method).toBe("POST")
    expect(auth(f.lastInit())).toBe("Bearer zt")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ device: { name: "Safari on iPhone", platform: "web" } })
  })

  test("refreshSession posts the refresh token", async () => {
    const f = mockFetch(() => reply(session))
    expect(await new CloudClient(BASE, f).refreshSession("wcr_old")).toEqual(session)
    expect(f.lastUrl()).toBe(`${BASE}/v1/session/refresh`)
    expect(f.lastInit().method).toBe("POST")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ refresh_token: "wcr_old" })
    expect(auth(f.lastInit())).toBeUndefined()
  })

  test("refreshSession waits 60 s before giving up", async () => {
    vi.useFakeTimers()
    try {
      let aborted = false
      const f = mockFetch((_url, init) => new Promise<Response>((_resolve, reject) => init.signal?.addEventListener("abort", () => { aborted = true; reject(new Error("aborted")) })))
      const pending = new CloudClient(BASE, f).refreshSession("wcr_old").catch((x) => x)
      await vi.advanceTimersByTimeAsync(59_000)
      expect(aborted).toBe(false)
      await vi.advanceTimersByTimeAsync(2_000)
      expect(aborted).toBe(true)
      expect(await pending).toBeInstanceOf(TimeoutError)
    } finally {
      vi.useRealTimers()
    }
  })

  test("devices lists with the access token; removeDevice treats 404 as done", async () => {
    const device = { id: "d1", name: "iPhone", platform: "ios", created_at: 1, last_seen_at: null, current: true }
    const f = mockFetch((_url, init) => (init.method === "DELETE" ? reply({ error: "not_found", message: "no" }, 404) : reply({ devices: [device] })))
    const cloud = new CloudClient(BASE, f)
    expect(await cloud.devices("at")).toEqual([device])
    expect(f.lastUrl()).toBe(`${BASE}/v1/devices`)
    expect(auth(f.lastInit())).toBe("Bearer at")
    await expect(cloud.removeDevice("at", "d/1")).resolves.toBeUndefined()
    expect(f.lastUrl()).toBe(`${BASE}/v1/devices/d%2F1`)
    expect(f.lastInit().method).toBe("DELETE")
    const broken = mockFetch(() => reply({ error: "boom", message: "x" }, 500))
    await expect(new CloudClient(BASE, broken).removeDevice("at", "d1")).rejects.toMatchObject({ status: 500 })
  })

  test("redeemInvite returns the plan", async () => {
    const plan = { plan: "beta", source: "invite", expires_at: 123 }
    const f = mockFetch(() => reply(plan))
    expect(await new CloudClient(BASE, f).redeemInvite("at", "CODE")).toEqual(plan)
    expect(f.lastUrl()).toBe(`${BASE}/v1/entitlements/redeem`)
    expect(f.lastInit().method).toBe("POST")
    expect(auth(f.lastInit())).toBe("Bearer at")
    expect(JSON.parse(String(f.lastInit().body))).toEqual({ code: "CODE" })
  })

  test("account carries plan, identities and key_set", async () => {
    const body = { account: "a", created_at: "t", servers: 0, plan: { plan: "beta", source: "invite", expires_at: null }, identities: ["zitadel"], key_set: false }
    const f = mockFetch(() => reply(body))
    expect(await new CloudClient(BASE, f).account("at")).toEqual(body)
  })

  test("ApiError keeps the error code", async () => {
    const f = mockFetch(() => reply({ error: "limit", message: "device limit" }, 403))
    const e = await new CloudClient(BASE, f).exchangeSignIn("zt", "n").catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(403)
    expect(e.code).toBe("limit")
  })
})
