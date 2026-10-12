import { afterEach, describe, expect, test, vi } from "vitest"
import { loadConfig } from "./config"

function serving(body: unknown, init?: ResponseInit): typeof fetch {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  return vi.fn(async () => new Response(text, init)) as unknown as typeof fetch
}

afterEach(() => vi.restoreAllMocks())

describe("config", () => {
  test("config: empty cloud url means no account", async () => {
    expect(await loadConfig(serving({ cloudUrl: "" }))).toEqual({ cloudUrl: null })
  })

  test("config: http cloud outside localhost is ignored", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {})
    expect(await loadConfig(serving({ cloudUrl: "http://cloud.example" }))).toEqual({ cloudUrl: null })
  })

  test("config: https cloud url is kept without a trailing slash", async () => {
    expect(await loadConfig(serving({ cloudUrl: "https://cloud.example" }))).toEqual({ cloudUrl: "https://cloud.example" })
    expect(await loadConfig(serving({ cloudUrl: "https://Cloud.Example:8443/" }))).toEqual({ cloudUrl: "https://cloud.example:8443" })
  })

  test("config: http cloud on loopback is allowed", async () => {
    expect(await loadConfig(serving({ cloudUrl: "http://localhost:8090" }))).toEqual({ cloudUrl: "http://localhost:8090" })
    expect(await loadConfig(serving({ cloudUrl: "http://127.0.0.1:8090" }))).toEqual({ cloudUrl: "http://127.0.0.1:8090" })
  })

  test("config: path, query, user and fragment are refused", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {})
    for (const cloudUrl of ["https://cloud.example/api", "https://cloud.example/?a=1", "https://u:p@cloud.example", "https://cloud.example/#x"]) {
      expect(await loadConfig(serving({ cloudUrl })), cloudUrl).toEqual({ cloudUrl: null })
    }
  })

  test("config: missing, wrong type or unreadable file means no account", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {})
    expect(await loadConfig(serving({}))).toEqual({ cloudUrl: null })
    expect(await loadConfig(serving({ cloudUrl: 5 }))).toEqual({ cloudUrl: null })
    expect(await loadConfig(serving("not json"))).toEqual({ cloudUrl: null })
    expect(await loadConfig(serving("", { status: 404 }))).toEqual({ cloudUrl: null })
    const failing = vi.fn(async () => {
      throw new TypeError("offline")
    }) as unknown as typeof fetch
    expect(await loadConfig(failing)).toEqual({ cloudUrl: null })
  })

  test("config: a refused value is never logged", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    await loadConfig(serving({ cloudUrl: "http://secret-host.example/x" }))
    expect(warn).toHaveBeenCalled()
    expect(JSON.stringify(warn.mock.calls)).not.toContain("secret-host")
  })

  test("config: reads /config.json without the HTTP cache", async () => {
    const fake = serving({ cloudUrl: "" })
    await loadConfig(fake)
    expect(fake).toHaveBeenCalledWith("/config.json", { cache: "no-store", signal: expect.any(AbortSignal) })
  })

  test("config: a request that hangs is aborted after 15 s and means no account", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    try {
      const hanging = vi.fn(
        (_url: unknown, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))
          }),
      ) as unknown as typeof fetch
      const result = loadConfig(hanging)
      await vi.advanceTimersByTimeAsync(14_999)
      let settled = false
      void result.then(() => (settled = true))
      await Promise.resolve()
      expect(settled).toBe(false)
      await vi.advanceTimersByTimeAsync(1)
      expect(await result).toEqual({ cloudUrl: null })
    } finally {
      vi.useRealTimers()
    }
  })
})
