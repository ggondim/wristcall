import { describe, expect, test } from "vitest"
import { mockFetch } from "../test/mockFetch"
import { RelayClient } from "./relay"

const BASE = "https://cloud.example"
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
const auth = (init: RequestInit) => (init.headers as Record<string, string>).Authorization
const SUBSCRIPTION = { endpoint: "https://push.example/abc", keys: { p256dh: "k", auth: "a" } }

describe("RelayClient", () => {
  test("register: webpush body, device.approval, label cut to 64, no token", async () => {
    const registration = { push_key: "wc_push_x", platform: "webpush", label: "l", tag: "home", events: ["device.approval"] }
    const f = mockFetch(() => reply(registration, 201))
    const result = await new RelayClient(`${BASE}/`, f).register(SUBSCRIPTION, "x".repeat(100), "home")
    expect(result).toEqual(registration)
    expect(f.lastUrl()).toBe(`${BASE}/v1/push/registrations`)
    expect(f.lastInit().method).toBe("POST")
    expect(auth(f.lastInit())).toBeUndefined()
    const body = JSON.parse(String(f.lastInit().body))
    expect(body).toEqual({ platform: "webpush", subscription: SUBSCRIPTION, label: "x".repeat(64), tag: "home", events: ["device.approval"] })
  })

  test("register: errors pass through", async () => {
    const f = mockFetch(() => reply({ error: "not_configured", message: "no channel" }, 404))
    await expect(new RelayClient(BASE, f).register(SUBSCRIPTION, "l", "t")).rejects.toMatchObject({ status: 404, code: "not_configured" })
  })

  test("check: 200 is ok with the push key as bearer", async () => {
    const f = mockFetch(() => reply({ platform: "webpush", label: "l", tag: "t", events: [] }))
    expect(await new RelayClient(BASE, f).check("wc_push_x")).toBe("ok")
    expect(f.lastUrl()).toBe(`${BASE}/v1/push/registrations/current`)
    expect(auth(f.lastInit())).toBe("Bearer wc_push_x")
  })

  test("relayCheckGone", async () => {
    const f = mockFetch(() => reply({ error: "gone", message: "unknown key" }, 410))
    expect(await new RelayClient(BASE, f).check("wc_push_x")).toBe("gone")
  })

  test("check: other failures throw", async () => {
    const f = mockFetch(() => reply({ error: "push_unavailable", message: "db" }, 503))
    await expect(new RelayClient(BASE, f).check("wc_push_x")).rejects.toMatchObject({ status: 503 })
  })

  test("unregister: 204 and relayUnregister404IsDone", async () => {
    const ok = mockFetch(() => new Response(null, { status: 204 }))
    await new RelayClient(BASE, ok).unregister("wc_push_x")
    expect(ok.lastInit().method).toBe("DELETE")
    expect(auth(ok.lastInit())).toBe("Bearer wc_push_x")
    const missing = mockFetch(() => reply({ error: "not_found", message: "no" }, 404))
    await expect(new RelayClient(BASE, missing).unregister("wc_push_x")).resolves.toBeUndefined()
    const bad = mockFetch(() => reply({ error: "x", message: "y" }, 500))
    await expect(new RelayClient(BASE, bad).unregister("wc_push_x")).rejects.toMatchObject({ status: 500 })
  })
})
