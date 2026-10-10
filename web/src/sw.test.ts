import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"

const ORIGIN = "https://app.example"

class FakeCache {
  readonly entries = new Map<string, Response>()
  readonly put = vi.fn(async (request: RequestInfo | URL, response: Response) => {
    this.entries.set(FakeCache.key(request), response)
  })
  static key(request: RequestInfo | URL): string {
    const url = typeof request === "string" ? request : request instanceof URL ? request.href : request.url
    return new URL(url, ORIGIN).pathname
  }
  async addAll(urls: string[]) {
    for (const url of urls) this.entries.set(FakeCache.key(url), await fetch(url))
  }
  async match(request: RequestInfo | URL) {
    return this.entries.get(FakeCache.key(request))?.clone()
  }
}

class FakeCaches {
  readonly stores = new Map<string, FakeCache>()
  async open(name: string) {
    if (!this.stores.has(name)) this.stores.set(name, new FakeCache())
    return this.stores.get(name)!
  }
  async keys() {
    return [...this.stores.keys()]
  }
  async delete(name: string) {
    return this.stores.delete(name)
  }
}

type Handler = (event: Record<string, unknown>) => void

/** Imports src/sw.ts with a fake worker scope; returns what it exported and a way to fire its events. */
async function loadServiceWorker(globals: { assets?: string[]; cache?: string } = {}) {
  const handlers = new Map<string, Handler>()
  const claim = vi.fn(async () => {})
  const caches = new FakeCaches()
  const fetchMock = vi.fn(async (_request: unknown) => new Response("from network"))
  vi.stubGlobal("self", {
    location: new URL(`${ORIGIN}/sw.js`),
    clients: { claim },
    addEventListener: (type: string, handler: Handler) => handlers.set(type, handler),
  })
  vi.stubGlobal("caches", caches)
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("__WC_ASSETS", globals.assets ?? ["assets/index-abc.js", "assets/index-abc.css"])
  vi.stubGlobal("__WC_CACHE", globals.cache ?? "wristcall-shell-v1")
  vi.resetModules()
  const sw = await import("./sw")

  async function lifecycle(type: "install" | "activate") {
    const waits: Promise<unknown>[] = []
    handlers.get(type)!({ waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
  }
  /** Fires a fetch event; null when the worker did not take it. */
  async function request(init: { url: string; mode?: string; method?: string }): Promise<Response | null> {
    const req = { url: init.url, mode: init.mode ?? "cors", method: init.method ?? "GET" }
    let responded: Promise<Response> | null = null
    handlers.get("fetch")!({ request: req, respondWith: (p: Promise<Response>) => (responded = p) })
    return responded
  }
  return { sw, handlers, claim, caches, fetchMock, lifecycle, request }
}

beforeEach(() => vi.resetModules())
afterEach(() => vi.unstubAllGlobals())

describe("service worker", () => {
  test("sw: api requests are never cached", async () => {
    const { sw } = await loadServiceWorker()
    expect(sw.handlesFetch(new Request("https://server.example/v1/agents"))).toBe(false)
    expect(sw.handlesFetch(new Request(ORIGIN + "/config.json"))).toBe(false)
    expect(sw.handlesFetch(new Request(ORIGIN + "/v1/agents"))).toBe(false)
    expect(sw.handlesFetch({ url: ORIGIN + "/assets/a.js", method: "POST", mode: "cors" })).toBe(false)
    expect(sw.handlesFetch({ url: "https://server.example/", method: "GET", mode: "navigate" })).toBe(false)
    expect(sw.handlesFetch({ url: ORIGIN + "/config.json", method: "GET", mode: "navigate" })).toBe(false)
  })

  test("sw: takes navigations and assets of its own origin only", async () => {
    const { sw } = await loadServiceWorker()
    expect(sw.handlesFetch({ url: ORIGIN + "/servers", method: "GET", mode: "navigate" })).toBe(true)
    expect(sw.handlesFetch(new Request(ORIGIN + "/assets/index-abc.js"))).toBe(true)
  })

  test("sw: a request it does not take is left to the browser", async () => {
    const { request, fetchMock } = await loadServiceWorker()
    expect(await request({ url: "https://server.example/v1/agents" })).toBeNull()
    expect(await request({ url: ORIGIN + "/config.json" })).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test("sw: install caches the shell and the listed assets", async () => {
    const { lifecycle, caches, fetchMock } = await loadServiceWorker()
    await lifecycle("install")
    const store = caches.stores.get("wristcall-shell-v1")!
    expect([...store.entries.keys()].sort()).toEqual(
      ["/", "/assets/index-abc.css", "/assets/index-abc.js", "/index.html", "/manifest.webmanifest"].sort(),
    )
    expect(fetchMock).toHaveBeenCalledTimes(5)
  })

  test("sw: activate deletes older shell caches only, and claims the open pages", async () => {
    const { lifecycle, caches, claim } = await loadServiceWorker()
    await caches.open("wristcall-shell-v0")
    await caches.open("wristcall-shell-v1")
    await caches.open("other-app-cache")
    await lifecycle("activate")
    expect((await caches.keys()).sort()).toEqual(["other-app-cache", "wristcall-shell-v1"])
    expect(claim).toHaveBeenCalledTimes(1)
  })

  test("sw: navigation goes to the network first", async () => {
    const { request, lifecycle, fetchMock } = await loadServiceWorker()
    await lifecycle("install")
    fetchMock.mockClear()
    const response = await (await request({ url: ORIGIN + "/servers", mode: "navigate" }))!
    expect(await response.text()).toBe("from network")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  test("sw: navigation without network falls back to the cached index.html", async () => {
    const { request, lifecycle, caches, fetchMock } = await loadServiceWorker()
    fetchMock.mockImplementation(async (r: unknown) => new Response(String((r as { url?: string }).url ?? r).endsWith("/index.html") ? "shell" : "other"))
    await lifecycle("install")
    fetchMock.mockImplementation(async () => {
      throw new TypeError("offline")
    })
    const response = await (await request({ url: ORIGIN + "/history", mode: "navigate" }))!
    expect(await response.text()).toBe("shell")
    expect(caches.stores.size).toBe(1)
  })

  test("swNeverCachesNavigation", async () => {
    const { request, lifecycle, caches } = await loadServiceWorker()
    await lifecycle("install")
    const store = caches.stores.get("wristcall-shell-v1")!
    store.put.mockClear()
    const before = [...store.entries.keys()].sort()
    await (await request({ url: ORIGIN + "/auth/callback?code=secret&state=x", mode: "navigate" }))!
    await (await request({ url: ORIGIN + "/servers", mode: "navigate" }))!
    expect(store.put).not.toHaveBeenCalled()
    expect([...store.entries.keys()].sort()).toEqual(before)
  })

  test("sw: assets come from the cache first and a miss is not stored", async () => {
    const { request, lifecycle, caches, fetchMock } = await loadServiceWorker()
    await lifecycle("install")
    const store = caches.stores.get("wristcall-shell-v1")!
    fetchMock.mockClear()
    const hit = await (await request({ url: ORIGIN + "/assets/index-abc.js" }))!
    expect(await hit.text()).toBe("from network") // what install stored
    expect(fetchMock).not.toHaveBeenCalled()
    await (await request({ url: ORIGIN + "/assets/other-def.js" }))!
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(store.put).not.toHaveBeenCalled()
  })

  test("sw: without a build it caches just the shell, under the dev name", async () => {
    const handlers = new Map<string, Handler>()
    const devCaches = new FakeCaches()
    vi.stubGlobal("self", { location: new URL(ORIGIN), clients: { claim: async () => {} }, addEventListener: (t: string, h: Handler) => handlers.set(t, h) })
    vi.stubGlobal("caches", devCaches)
    vi.stubGlobal("fetch", async () => new Response("x"))
    vi.resetModules()
    await import("./sw")
    expect([...handlers.keys()].sort()).toEqual(["activate", "fetch", "install"])
    const waits: Promise<unknown>[] = []
    handlers.get("install")!({ waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    expect([...devCaches.stores.keys()]).toEqual(["wristcall-shell-dev"])
    expect([...devCaches.stores.get("wristcall-shell-dev")!.entries.keys()].sort()).toEqual(["/", "/index.html", "/manifest.webmanifest"])
  })
})
