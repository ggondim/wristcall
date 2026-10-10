import { describe, expect, test } from "vitest"
import { ApiError } from "../lib/errors"
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
    expect(() => cloud.updateServer("at", "..", {})).toThrow()
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
})
