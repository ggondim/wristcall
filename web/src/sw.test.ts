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
  const showNotification = vi.fn(async (_title: string, _options?: NotificationOptions) => {})
  const windows: Record<string, unknown>[] = []
  const matchAll = vi.fn(async (_options?: unknown) => windows)
  const openWindow = vi.fn(async (_url: string) => null)
  vi.stubGlobal("self", {
    location: new URL(`${ORIGIN}/sw.js`),
    clients: { claim, matchAll, openWindow },
    registration: { showNotification },
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
  /** Fires a push event with the given text (null: a push without data); resolves when the worker is done with it. */
  async function push(text: string | null | undefined): Promise<void> {
    const waits: Promise<unknown>[] = []
    const data = text === undefined ? null : { text: () => text }
    handlers.get("push")!({ data: text === null ? { text: () => { throw new Error("unreadable") } } : data, waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
  }
  /** Fires a tap on a notification carrying `data`. */
  async function tap(data: unknown): Promise<{ close: ReturnType<typeof vi.fn> }> {
    const waits: Promise<unknown>[] = []
    const notification = { close: vi.fn(), data }
    handlers.get("notificationclick")!({ notification, waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    return notification
  }
  return { sw, handlers, claim, caches, fetchMock, lifecycle, request, showNotification, windows, matchAll, openWindow, push, tap }
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
    expect([...handlers.keys()].sort()).toEqual(["activate", "fetch", "install", "notificationclick", "push"])
    const waits: Promise<unknown>[] = []
    handlers.get("install")!({ waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    expect([...devCaches.stores.keys()]).toEqual(["wristcall-shell-dev"])
    expect([...devCaches.stores.get("wristcall-shell-dev")!.entries.keys()].sort()).toEqual(["/", "/index.html", "/manifest.webmanifest"])
  })
describe("service worker push", () => {
  const approval = JSON.stringify({
    v: 1,
    event: "device.approval",
    tag: "srv-1",
    title: "New device",
    subtitle: "home",
    body: "Gustavo's watch",
    data: { request_id: "0423", device_name: "Gustavo's watch", expires_at: 1_800_000_300 },
  })

  test("sw: a push shows the approval", async () => {
    const { push, showNotification } = await loadServiceWorker()
    await push(approval)
    expect(showNotification).toHaveBeenCalledTimes(1)
    const [title, options] = showNotification.mock.calls[0]!
    expect(title).toBe("New device")
    expect(options).toMatchObject({
      body: "home: Gustavo's watch",
      tag: "approval:srv-1:0423",
      data: { tag: "srv-1", requestId: "0423", expiresAt: 1_800_000_300 },
    })
    expect(options).not.toHaveProperty("actions")
  })

  test("swShowsNotificationForEveryPush", async () => {
    const { push, showNotification, handlers } = await loadServiceWorker()
    await push("not json")
    await push(JSON.stringify({ v: 1, event: "call.finished", tag: "x", data: {} }))
    await push(JSON.stringify({ ...JSON.parse(approval), data: { request_id: "12a4", expires_at: 5 } }))
    // A push without data, and one whose data cannot be read.
    const waits: Promise<unknown>[] = []
    handlers.get("push")!({ data: null, waitUntil: (p: Promise<unknown>) => waits.push(p) })
    handlers.get("push")!({ waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    await push(null)
    expect(showNotification).toHaveBeenCalledTimes(6)
    for (const call of showNotification.mock.calls) {
      expect(call[0]).toBe("wristcall")
      expect(call[1]).toMatchObject({ body: "Open wristcall to see what's new.", data: {} })
    }
  })

  test("a notification the browser refuses is shown as the generic one", async () => {
    const { push, showNotification } = await loadServiceWorker()
    showNotification.mockRejectedValueOnce(new TypeError("bad options"))
    await push(approval)
    expect(showNotification).toHaveBeenCalledTimes(2)
    expect(showNotification.mock.calls[1]![0]).toBe("wristcall")
  })

  test("every push tells the open windows to look at the approvals", async () => {
    const { push, windows } = await loadServiceWorker()
    const postMessage = vi.fn()
    windows.push({ url: ORIGIN + "/servers", postMessage })
    await push(approval)
    await push("not json")
    expect(postMessage).toHaveBeenCalledTimes(2)
    expect(postMessage).toHaveBeenCalledWith({ kind: "approvals" })
  })

  test("swNeverFetches", async () => {
    const { push, tap, windows, fetchMock } = await loadServiceWorker()
    windows.push({ url: ORIGIN + "/servers", focus: vi.fn(), postMessage: vi.fn(), focused: true })
    fetchMock.mockClear()
    await push(approval)
    await push("not json")
    await tap({ tag: "srv-1", requestId: "0423", expiresAt: 5 })
    await tap({})
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test("swClickFocusesExistingWindow", async () => {
    const { tap, windows, openWindow } = await loadServiceWorker()
    const focus = vi.fn(async () => {})
    const postMessage = vi.fn()
    windows.push({ url: ORIGIN + "/servers", focus, postMessage, focused: true, visibilityState: "visible" })
    const notification = await tap({ tag: "srv-1", requestId: "0423", expiresAt: 5 })
    expect(notification.close).toHaveBeenCalledTimes(1)
    expect(focus).toHaveBeenCalledTimes(1)
    expect(postMessage).toHaveBeenCalledWith({ kind: "open", target: "/approvals?server=srv-1&request=0423&expires=5" })
    expect(openWindow).not.toHaveBeenCalled()
  })

  test("a tap prefers the window in front, and still works if focus is refused", async () => {
    const { tap, windows, openWindow } = await loadServiceWorker()
    const back = { url: ORIGIN + "/", focus: vi.fn(async () => {}), postMessage: vi.fn(), focused: false }
    const front = { url: ORIGIN + "/history", focus: vi.fn(async () => { throw new Error("refused") }), postMessage: vi.fn(), focused: true }
    windows.push(back, front)
    await tap({})
    expect(back.postMessage).not.toHaveBeenCalled()
    expect(front.postMessage).toHaveBeenCalledWith({ kind: "open", target: "/approvals" })
    expect(openWindow).not.toHaveBeenCalled()
  })

  test("clickOpensApprovalsOnly in the worker: nothing from the data leaves the app", async () => {
    const { tap, windows, openWindow } = await loadServiceWorker()
    const postMessage = vi.fn()
    windows.push({ url: ORIGIN + "/", focus: vi.fn(async () => {}), postMessage })
    await tap({ url: "https://evil.example" })
    expect(postMessage).toHaveBeenCalledWith({ kind: "open", target: "/approvals" })
    windows.length = 0
    await tap({ url: "https://evil.example", tag: "srv-1", requestId: "0423", expiresAt: 5 })
    expect(openWindow).toHaveBeenCalledWith("/approvals?server=srv-1&request=0423&expires=5")
  })

  test("without an open window a tap opens one", async () => {
    const { tap, openWindow } = await loadServiceWorker()
    await tap({ tag: "srv-1", requestId: "0423", expiresAt: 5 })
    expect(openWindow).toHaveBeenCalledTimes(1)
    expect(openWindow).toHaveBeenCalledWith("/approvals?server=srv-1&request=0423&expires=5")
  })

  test("a window of another origin is left alone", async () => {
    const { tap, windows, openWindow } = await loadServiceWorker()
    const postMessage = vi.fn()
    windows.push({ url: "https://evil.example/", focus: vi.fn(), postMessage })
    await tap({})
    expect(postMessage).not.toHaveBeenCalled()
    expect(openWindow).toHaveBeenCalledWith("/approvals")
  })
})
})
