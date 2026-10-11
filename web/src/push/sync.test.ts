import { beforeEach, describe, expect, test, vi } from "vitest"
import { ApiError } from "../lib/errors"
import { openDb } from "../store/db"
import { type SavedServer, ServerStore } from "../store/servers"
import { PushSync } from "./sync"
import type { ServerClient } from "../api/server"

const CLOUD = "https://cloud.example"

function server(over: Partial<SavedServer> = {}): SavedServer {
  return {
    id: "srv-1",
    name: "home",
    url: "https://home.example",
    token: "wc_pat_secret",
    linked: true,
    cloudServerId: null,
    pushKey: null,
    pushEndpoint: null,
    addedAt: 1,
    ...over,
  }
}

function subscriptionWith(endpoint: string): PushSubscription {
  const json = { endpoint, keys: { p256dh: "p", auth: "a" } }
  return { endpoint, toJSON: () => json } as unknown as PushSubscription
}

/** A management API that records what the sync does to it. */
class FakeServer {
  health: { account: unknown; push: { relay: string } | null } | Error = { account: { issuer: CLOUD, device_credential: "x" }, push: { relay: CLOUD } }
  lastPushKey: string | null = null
  cleared = 0
  failPut = false
  readonly setPushKey = vi.fn(async (key: string) => {
    if (this.failPut) throw new ApiError(500, "boom", "boom")
    this.lastPushKey = key
  })
  readonly clearPushKey = vi.fn(async () => {
    this.cleared++
    this.lastPushKey = null
  })
  readonly client = {
    health: async () => {
      if (this.health instanceof Error) throw this.health
      return { status: "ok", version: "0.7.0", protocol: 1, ...this.health }
    },
    setPushKey: this.setPushKey,
    clearPushKey: this.clearPushKey,
  } as unknown as ServerClient
}

/** A relay that hands out `wc_push_new`, `wc_push_new2`, ... and knows which keys are alive. */
class FakeRelay {
  alive = new Set<string>()
  count = 0
  readonly register = vi.fn(async (_subscription: PushSubscriptionJSON, _label: string, _tag: string) => {
    const key = this.count++ === 0 ? "wc_push_new" : `wc_push_new${this.count}`
    this.alive.add(key)
    return { push_key: key, platform: "webpush" as const, label: "x", tag: "x", events: ["device.approval"] }
  })
  readonly check = vi.fn(async (key: string): Promise<"ok" | "gone"> => (this.alive.has(key) ? "ok" : "gone"))
  readonly unregister = vi.fn(async (key: string) => {
    this.alive.delete(key)
  })
}

let store: ServerStore
let relay: FakeRelay
let fake: FakeServer
const sync = () => new PushSync(store, relay, () => fake.client, CLOUD)

beforeEach(async () => {
  store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  relay = new FakeRelay()
  fake = new FakeServer()
})

describe("PushSync.syncAll", () => {
  test("a server without a key is registered, given the key, and remembers it", async () => {
    await store.put(server())
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.register).toHaveBeenCalledTimes(1)
    expect(relay.register).toHaveBeenCalledWith({ endpoint: "https://fcm.googleapis.com/a", keys: { p256dh: "p", auth: "a" } }, "home", "srv-1")
    expect(fake.lastPushKey).toBe("wc_push_new")
    expect(await store.get("srv-1")).toMatchObject({ pushKey: "wc_push_new", pushEndpoint: "https://fcm.googleapis.com/a" })
  })

  test("sync skips servers with another relay", async () => {
    await store.put(server())
    fake.health = { account: { issuer: CLOUD, device_credential: "x" }, push: { relay: "https://other.example" } }
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.register).not.toHaveBeenCalled()
    expect(fake.setPushKey).not.toHaveBeenCalled()
  })

  test("the relay compares as an address: slash and case do not matter", async () => {
    await store.put(server())
    fake.health = { account: { issuer: CLOUD, device_credential: "x" }, push: { relay: "HTTPS://Cloud.example/" } }
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.register).toHaveBeenCalledTimes(1)
  })

  test("a server without push, without an account or not answering is skipped quietly", async () => {
    await store.put(server({ id: "a" }))
    await store.put(server({ id: "b", addedAt: 2 }))
    await store.put(server({ id: "c", addedAt: 3 }))
    const clients: Record<string, ServerClient> = {}
    for (const [id, health] of [
      ["a", { account: { issuer: CLOUD, device_credential: "x" }, push: null }],
      ["b", { account: null, push: { relay: CLOUD } }],
      ["c", new Error("down")],
    ] as const) {
      const f = new FakeServer()
      f.health = health
      clients[id] = f.client
    }
    const all = new PushSync(store, relay, (s) => clients[s.id]!, CLOUD)
    await expect(all.syncAll(subscriptionWith("https://fcm.googleapis.com/a"))).resolves.toBeUndefined()
    expect(relay.register).not.toHaveBeenCalled()
  })

  test("one server failing does not stop the others", async () => {
    await store.put(server({ id: "a" }))
    await store.put(server({ id: "b", addedAt: 2 }))
    const bad = new FakeServer()
    bad.health = new Error("down")
    const good = new FakeServer()
    const all = new PushSync(store, relay, (s) => (s.id === "a" ? bad : good).client, CLOUD)
    await all.syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect((await store.get("b"))?.pushKey).toBe("wc_push_new")
    expect((await store.get("a"))?.pushKey).toBeNull()
  })

  test("sync re-registers when subscription changed", async () => {
    await store.put(server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/old" }))
    relay.alive.add("wc_push_old")
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/new"))
    expect(relay.unregister).toHaveBeenCalledWith("wc_push_old")
    expect(fake.lastPushKey).toBe("wc_push_new")
    expect(await store.get("srv-1")).toMatchObject({ pushKey: "wc_push_new", pushEndpoint: "https://fcm.googleapis.com/new" })
  })

  test("syncGoneRegistersAgain", async () => {
    await store.put(server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/a" })) // the relay forgot it
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.check).toHaveBeenCalledWith("wc_push_old")
    expect(relay.register).toHaveBeenCalledTimes(1)
    expect(fake.lastPushKey).toBe("wc_push_new")
    expect(await store.get("srv-1")).toMatchObject({ pushKey: "wc_push_new" })
  })

  test("a key the relay still knows is left alone", async () => {
    await store.put(server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/a" }))
    relay.alive.add("wc_push_old")
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.register).not.toHaveBeenCalled()
    expect(fake.setPushKey).not.toHaveBeenCalled()
    expect(relay.unregister).not.toHaveBeenCalled()
  })

  test("syncWithoutSubscriptionDoesNothing", async () => {
    await store.put(server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/a" }))
    await sync().syncAll(null)
    expect(relay.check).not.toHaveBeenCalled()
    expect(relay.register).not.toHaveBeenCalled()
    expect(fake.setPushKey).not.toHaveBeenCalled()
    expect(await store.get("srv-1")).toMatchObject({ pushKey: "wc_push_old" })
  })

  test("a key the server refused is dropped at the relay and not kept", async () => {
    await store.put(server())
    fake.failPut = true
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.unregister).toHaveBeenCalledWith("wc_push_new")
    expect(await store.get("srv-1")).toMatchObject({ pushKey: null, pushEndpoint: null })
  })

  test("a server removed while the relay answered gets no key", async () => {
    await store.put(server())
    relay.register.mockImplementationOnce(async () => {
      await store.remove("srv-1")
      relay.alive.add("wc_push_new")
      return { push_key: "wc_push_new", platform: "webpush", label: "home", tag: "srv-1", events: [] }
    })
    await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(fake.setPushKey).not.toHaveBeenCalled()
    expect(relay.unregister).toHaveBeenCalledWith("wc_push_new")
  })

  test("two syncs at once register once", async () => {
    await store.put(server())
    const one = sync()
    const subscription = subscriptionWith("https://fcm.googleapis.com/a")
    await Promise.all([one.syncAll(subscription), one.syncAll(subscription)])
    expect(relay.register).toHaveBeenCalledTimes(1)
  })

  test("the sync runs under the push lock when the browser has locks", async () => {
    const request = vi.fn(async (_name: string, work: () => Promise<unknown>) => work())
    vi.stubGlobal("navigator", { ...globalThis.navigator, locks: { request } })
    try {
      await store.put(server())
      await sync().syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
      expect(request).toHaveBeenCalledTimes(1)
      expect(request.mock.calls[0]![0]).toBe("wc.push")
      expect(relay.register).toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe("PushSync.forget", () => {
  test("it deletes the key at the server and the relay, and clears it", async () => {
    const saved = server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/a" })
    await store.put(saved)
    relay.alive.add("wc_push_old")
    await sync().forget(saved)
    expect(fake.clearPushKey).toHaveBeenCalledTimes(1)
    expect(relay.unregister).toHaveBeenCalledWith("wc_push_old")
    expect(await store.get("srv-1")).toMatchObject({ pushKey: null, pushEndpoint: null })
  })

  test("forgetIsBestEffort", async () => {
    const saved = server({ pushKey: "wc_push_old", pushEndpoint: "https://fcm.googleapis.com/a" })
    await store.put(saved)
    fake.clearPushKey.mockRejectedValueOnce(new Error("down"))
    relay.unregister.mockRejectedValueOnce(new Error("down"))
    await expect(sync().forget(saved)).resolves.toBeUndefined()
    expect(relay.unregister).toHaveBeenCalledTimes(1)
    expect(await store.get("srv-1")).toMatchObject({ pushKey: null })
  })

  test("a server without a key has nothing to forget", async () => {
    await sync().forget(server())
    expect(fake.clearPushKey).not.toHaveBeenCalled()
    expect(relay.unregister).not.toHaveBeenCalled()
  })

  test("serverCleared leaves the server alone and a removed server stays removed", async () => {
    const saved = server({ pushKey: "wc_push_old" })
    await sync().forget(saved, { serverCleared: true })
    expect(fake.clearPushKey).not.toHaveBeenCalled()
    expect(relay.unregister).toHaveBeenCalledWith("wc_push_old")
    expect(await store.get("srv-1")).toBeNull()
  })
})

describe("PushSync.accountDeleted", () => {
  test("servers that hang are forgotten at the same time", async () => {
    await store.put(server({ id: "a", pushKey: "wc_push_a", pushEndpoint: "e" }))
    await store.put(server({ id: "b", addedAt: 2, pushKey: "wc_push_b", pushEndpoint: "e" }))
    let started = 0
    const release: Array<() => void> = []
    const hang = (): Promise<void> => {
      started++
      return new Promise((resolve) => release.push(resolve))
    }
    const hanging = { clearPushKey: hang } as unknown as ServerClient
    const all = new PushSync(store, relay, () => hanging, CLOUD)
    const done = all.accountDeleted()
    await vi.waitFor(() => expect(started).toBe(2)) // both started before either finished
    release.forEach((resolve) => resolve())
    await done
    expect((await store.list()).map((s) => s.pushKey)).toEqual([null, null])
  })

  test("every key is undone, and nothing is registered again", async () => {
    await store.put(server({ id: "a", pushKey: "wc_push_a", pushEndpoint: "https://fcm.googleapis.com/a" }))
    await store.put(server({ id: "b", addedAt: 2, pushKey: "wc_push_b", pushEndpoint: "https://fcm.googleapis.com/a" }))
    await store.put(server({ id: "c", addedAt: 3 }))
    const all = sync()
    await all.accountDeleted()
    expect(relay.unregister.mock.calls.map((call) => call[0]).sort()).toEqual(["wc_push_a", "wc_push_b"])
    expect(fake.clearPushKey).toHaveBeenCalledTimes(2)
    expect((await store.list()).map((s) => s.pushKey)).toEqual([null, null, null])
    await all.syncAll(subscriptionWith("https://fcm.googleapis.com/a"))
    expect(relay.register).not.toHaveBeenCalled()
  })
})
