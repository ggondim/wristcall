import { afterEach, describe, expect, test, vi } from "vitest"
import { mockFetch } from "../test/mockFetch"
import { ApiError, NetworkError, TimeoutError, userMessage } from "./errors"
import { json, request } from "./http"

afterEach(() => vi.useRealTimers())

describe("http", () => {
  test("http: error never carries the token", async () => {
    const fetchMock = mockFetch(() => new Response(JSON.stringify({ error: "unauthorized", message: "bad" }), { status: 401 }))
    const e = await request("https://s.example/v1/agents", { bearer: "wc_pat_secret" }, fetchMock).catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(401)
    expect(e.code).toBe("unauthorized")
    expect(JSON.stringify(e) + String(e) + e.stack).not.toContain("wc_pat_secret")
  })

  test("ApiError keeps Retry-After", async () => {
    const answer = (headers: Record<string, string>) => mockFetch(() => new Response(JSON.stringify({ error: "rate_limited", message: "slow" }), { status: 429, headers }))
    const seconds = await request("https://s.example/x", {}, answer({ "Retry-After": "7" })).catch((x) => x)
    expect(seconds).toBeInstanceOf(ApiError)
    expect(seconds.retryAfter).toBe(7)
    for (const value of [undefined, "", "soon", "-3", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT"]) {
      const e = await request("https://s.example/x", {}, answer(value === undefined ? {} : { "Retry-After": value })).catch((x) => x)
      expect(e.retryAfter).toBeNull()
    }
  })

  test("http: sends the bearer and a JSON body", async () => {
    const fetchMock = mockFetch(() => new Response("{}"))
    await json("https://s.example/v1/agents", { method: "POST", bearer: "wc_pat_x", json: { slug: "a" } }, fetchMock)
    const headers = fetchMock.lastInit().headers as Record<string, string>
    expect(headers.Authorization).toBe("Bearer wc_pat_x")
    expect(headers["Content-Type"]).toBe("application/json")
    expect(fetchMock.lastInit().body).toBe('{"slug":"a"}')
    expect(fetchMock.lastInit().credentials).toBe("omit")
    expect(fetchMock.lastInit().redirect).toBe("error")
    await request("https://s.example/v1/health", {}, fetchMock)
    const bare = fetchMock.lastInit().headers as Record<string, string>
    expect(bare.Authorization).toBeUndefined()
    expect(bare["Content-Type"]).toBeUndefined()
  })

  test("http: a redirect is refused, not followed, so the token goes nowhere else", async () => {
    // A real fetch rejects with a TypeError when `redirect: "error"` meets a redirect; the stand-in does the same.
    const fetchMock = mockFetch((_url, init) => {
      if (init?.redirect === "error") throw new TypeError("redirect")
      return new Response("{}")
    })
    await expect(json("https://s.example/v1/agents", { bearer: "wc_pat_x" }, fetchMock)).rejects.toBeInstanceOf(NetworkError)
  })

  test("http: a 204 has no body", async () => {
    const fetchMock = mockFetch(() => new Response(null, { status: 204 }))
    expect(await json("https://s.example/v1/push", { method: "DELETE" }, fetchMock)).toBeUndefined()
  })

  test("httpTimeoutAborts", async () => {
    // fake-indexeddb runs on setImmediate: only the timers the client uses are faked.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    let aborted = false
    const fetchMock = mockFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            aborted = true
            reject(new DOMException("aborted", "AbortError"))
          })
        }),
    )
    const pending = request("https://s.example/v1/agents", {}, fetchMock).catch((x) => x)
    await vi.advanceTimersByTimeAsync(14_999)
    expect(aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(2)
    expect(await pending).toBeInstanceOf(TimeoutError)
    expect(aborted).toBe(true)
  })

  test("http: timeout also holds for a fetch that ignores the signal", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    const fetchMock = mockFetch(() => new Promise<Response>(() => {}))
    const pending = request("https://s.example/v1/agents", { timeoutMs: 30_000 }, fetchMock).catch((x) => x)
    await vi.advanceTimersByTimeAsync(30_001)
    expect(await pending).toBeInstanceOf(TimeoutError)
  })

  test("httpMapsFastApiValidationError", async () => {
    const fetchMock = mockFetch(() => new Response(JSON.stringify({ detail: [{ msg: "field required" }] }), { status: 422 }))
    const e = (await json("https://s.example/v1/agents", { method: "POST", json: {} }, fetchMock).catch((x: unknown) => x)) as ApiError
    expect(e).toBeInstanceOf(ApiError)
    expect(e.code).toBe("invalid")
    expect(e.message).toBe("field required")
  })

  test("http: an error without a JSON body falls back to the status", async () => {
    const fetchMock = mockFetch(() => new Response("<html>", { status: 502, statusText: "Bad Gateway" }))
    const e = await request("https://s.example/v1/agents", {}, fetchMock).catch((x) => x)
    expect([e.status, e.code, e.message]).toEqual([502, "http_error", "Bad Gateway"])
  })

  test("httpNetworkErrorIsNetworkError", async () => {
    const fetchMock = mockFetch(() => Promise.reject(new TypeError("Failed to fetch https://s.example/?t=wc_pat_secret")))
    const e = await request("https://s.example/v1/agents", { bearer: "wc_pat_secret" }, fetchMock).catch((x) => x)
    expect(e).toBeInstanceOf(NetworkError)
    expect(String(e) + e.stack).not.toContain("wc_pat_secret")
  })

  test("http: a 200 that is not JSON is not a wristcall server", async () => {
    const fetchMock = mockFetch(() => new Response("<!doctype html><title>site</title>", { status: 200 }))
    const e = (await json("https://s.example/v1/health", {}, fetchMock).catch((x: unknown) => x)) as ApiError
    expect(e).toBeInstanceOf(ApiError)
    expect([e.status, e.code]).toEqual([200, "bad_response"])
    expect(userMessage(e)).toBe("This is not a wristcall server.")
  })

  test("http: user messages", () => {
    expect(userMessage(new ApiError(401, "unauthorized", "x"))).toContain("revoked")
    expect(userMessage(new ApiError(401, "invalid_code", "x"))).toContain("code")
    expect(userMessage(new NetworkError())).toContain("CORS")
    expect(userMessage(new ApiError(422, "invalid", "Slug taken"))).toBe("Slug taken")
    expect(userMessage("weird")).toBe("Something went wrong.")
  })
})
