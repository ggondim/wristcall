import { describe, expect, test } from "vitest"
import { ApiError } from "../lib/errors"
import { ServerClient } from "../api/server"
import type { CloudServer } from "../api/cloud"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { CloudRouter } from "../account/router"
import { CloudClient } from "../api/cloud"
import { RoutedServerClient } from "../api/routedServer"
import { userMessage } from "../lib/errors"
import { serversWith } from "../test/fakeServers"
import { CLOUD_ENTRY_ID, isCloudEntry, openServerStorage, ServersModel, unavailableStorage } from "./servers"

function server(over: Partial<SavedServer> = {}): SavedServer {
  return {
    id: "1",
    name: "home",
    url: "https://home.example.com",
    token: "wc_pat_secret",
    linked: false,
    cloudServerId: null,
    pushKey: null,
    pushEndpoint: null,
    addedAt: 1,
    ...over,
  }
}

function cloud(over: Partial<CloudServer> = {}): CloudServer {
  return { id: "c1", name: "Office", url: "https://office.example.com", kind: "self-hosted", linked: false, agents: [], created_at: "", updated_at: "", ...over }
}

const HEALTH = { status: "ok", version: "0.7.0", protocol: 1, account: null, push: null }
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })

/** A client per server whose fetch is `handler(host, path)`. */
function clients(handler: (host: string, path: string) => Response | Promise<Response>) {
  return (s: SavedServer) => {
    const url = new URL(s.url)
    return new ServerClient(url, s.token, mockFetch((u) => handler(new URL(u).host, new URL(u).pathname)))
  }
}

async function fresh(...saved: SavedServer[]): Promise<ServerStore> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of saved) await store.put(s)
  return store
}

describe("ServersModel", () => {
  test("loadMarksOfflineServer", async () => {
    const store = await fresh(server({ id: "1", url: "https://down.example.com" }), server({ id: "2", url: "https://up.example.com", addedAt: 2 }))
    const model = new ServersModel(
      store,
      clients((host, path) => {
        if (host === "down.example.com") throw new TypeError("offline")
        return path.endsWith("/health") ? ok(HEALTH) : ok({ providers: [], custom_endpoints: false })
      }),
    )
    await model.load()
    const [down, up] = model.state.rows
    expect(down).toMatchObject({ health: null, problem: "Can't reach host." })
    expect(up).toMatchObject({ health: { version: "0.7.0" }, problem: null })
    expect(model.state.loading).toBe(false)
  })

  test("loadMarksRejectedToken", async () => {
    const store = await fresh(server())
    const model = new ServersModel(
      store,
      clients((_, path) =>
        path.endsWith("/health") ? ok(HEALTH) : new Response(JSON.stringify({ error: { code: "unauthorized", message: "no" } }), { status: 401 }),
      ),
    )
    await model.load()
    expect(model.state.rows[0]).toMatchObject({ health: { version: "0.7.0" }, problem: "Token was revoked." })
  })

  test("loadKeepsEarlierAnswerWhileAsking", async () => {
    const store = await fresh(server())
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    let second = false
    const model = new ServersModel(
      store,
      clients(async (_, path) => {
        if (second) await gate
        return path.endsWith("/health") ? ok(HEALTH) : ok({ providers: [], custom_endpoints: false })
      }),
    )
    await model.load()
    second = true
    const again = model.load()
    await new Promise((r) => setTimeout(r, 10))
    expect(model.state.rows[0]?.health?.version).toBe("0.7.0")
    release()
    await again
  })

  test("renameRejectsEmptyAndControlChars", async () => {
    const store = await fresh(server())
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    await expect(model.rename("1", "   ")).rejects.toThrow("The name cannot be empty.")
    await expect(model.rename("1", "a\u0007b")).rejects.toThrow("control")
    await expect(model.rename("1", "x".repeat(65))).rejects.toThrow("64")
    expect((await store.get("1"))?.name).toBe("home")
    await model.rename("1", "  office  ")
    expect((await store.get("1"))?.name).toBe("office")
    await model.rename("1", "y".repeat(64))
    expect((await store.get("1"))?.name).toBe("y".repeat(64))
  })

  test("fromAccountHidesLocalServers", async () => {
    const store = await fresh(server({ url: "https://Home.Example.com:443/" }))
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    await model.load()
    model.setFromAccount([cloud({ id: "a", url: "https://home.example.com" }), cloud({ id: "b", url: "https://office.example.com" })])
    expect(model.state.fromAccount.map((c) => c.id)).toEqual(["b"])
    await model.remove("1")
    expect(model.state.fromAccount.map((c) => c.id)).toEqual(["a", "b"])
  })

  test("forgetAccountUnlinksEverythingAndKeepsTheServers", async () => {
    const store = await fresh(
      server({ id: "1", linked: true, cloudServerId: "c-1", pushKey: "wc_push_k" }),
      server({ id: "2", url: "https://office.example.com", token: "wc_pat_two", addedAt: 2 }),
    )
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    await model.load()
    model.setFromAccount([cloud({ id: "a", url: "https://other.example.com" })])
    const events: string[] = []
    model.onChange((event) => events.push(event.kind))
    await model.forgetAccount()
    expect(await store.get("1")).toMatchObject({ linked: false, cloudServerId: null, token: "wc_pat_secret", pushKey: "wc_push_k" })
    expect(await store.get("2")).toMatchObject({ token: "wc_pat_two", linked: false, cloudServerId: null })
    expect(model.state.rows.map((row) => row.server.linked)).toEqual([false, false])
    expect(model.state.fromAccount).toEqual([])
    expect(events).toEqual([]) // the agenda belongs to the account that left: nothing to mirror
  })

  test("unlinkAccountsUnlinksOnlyTheLinkedServersAndNeverFails", async () => {
    const store = await fresh(
      server({ id: "1", linked: true, cloudServerId: "c-1" }),
      server({ id: "2", url: "https://office.example.com", token: "wc_pat_two", addedAt: 2 }),
      server({ id: "3", url: "https://down.example.com", token: "wc_pat_three", linked: true, addedAt: 3 }),
    )
    const unlinked: string[] = []
    const model = new ServersModel(
      store,
      clients((host, path) => {
        if (host === "down.example.com") throw new TypeError("offline")
        if (path === "/v1/account/link") unlinked.push(host)
        return new Response(null, { status: 204 })
      }),
    )
    await expect(model.unlinkAccounts()).resolves.toBeUndefined()
    expect(unlinked).toEqual(["home.example.com"])
    expect(await store.get("1")).toMatchObject({ linked: true }) // the local link goes with the sign-out, not here
  })

  test("removeForgetsAndNotifiesThenCallsTheHook", async () => {
    const store = await fresh(server())
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    const order: string[] = []
    model.setBeforeRemove(async (s) => {
      order.push(`hook:${s.id}:${(await store.get(s.id)) === null ? "gone" : "present"}`)
      throw new ApiError(500, "x", "the hook failing does not keep the server")
    })
    model.onChange((event) => order.push(`${event.kind}:${event.server.id}`))
    await model.load()
    await model.remove("1")
    await model.cleanupsDone()
    expect(order).toEqual(["removed:1", "hook:1:gone"])
    expect(await store.list()).toEqual([])
    expect(model.state.rows).toEqual([])
  })

  test("renameNotifiesListenersAndClientUsesTheToken", async () => {
    const store = await fresh(server())
    const seen: string[] = []
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    const off = model.onChange((event) => seen.push(`${event.kind}:${event.server.name}`))
    await model.rename("1", "office")
    off()
    await model.rename("1", "again")
    expect(seen).toEqual(["updated:office"])
    expect(await model.client("1")).toBeInstanceOf(ServerClient)
    expect(await model.client("nope")).toBeNull()
  })

  test("loadDropsAListReadBeforeARemoval", async () => {
    const real = await fresh(server())
    let first = true
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    const store = {
      list: async () => {
        const snapshot = await real.list()
        if (first) {
          first = false
          await gate
        }
        return snapshot
      },
      get: (id: string) => real.get(id),
      put: (server: SavedServer) => real.put(server),
      remove: (id: string) => real.remove(id),
      update: (id: string, patch: Partial<SavedServer>) => real.update(id, patch),
    }
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    const loading = model.load()
    await new Promise((r) => setTimeout(r, 10))
    await model.remove("1")
    release()
    await loading
    expect(model.state.rows).toEqual([])
  })

  test("loadDropsAListReadBeforeARename", async () => {
    const real = await fresh(server())
    let first = true
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    const store = {
      list: async () => {
        const snapshot = await real.list()
        if (first) {
          first = false
          await gate
        }
        return snapshot
      },
      get: (id: string) => real.get(id),
      put: (server: SavedServer) => real.put(server),
      remove: (id: string) => real.remove(id),
      update: (id: string, patch: Partial<SavedServer>) => real.update(id, patch),
    }
    const model = new ServersModel(store, clients(() => ok(HEALTH)))
    const loading = model.load()
    await new Promise((r) => setTimeout(r, 10))
    await model.rename("1", "office")
    release()
    await loading
    expect(model.state.rows[0]?.server.name).toBe("office")
  })

  test("savedAddsARowAnnouncesItAndChecksIt", async () => {
    const store = await fresh()
    const model = new ServersModel(store, clients((_, path) => (path.endsWith("/health") ? ok(HEALTH) : ok({ providers: [], custom_endpoints: false }))))
    const seen: string[] = []
    model.onChange((event) => seen.push(`${event.kind}:${event.server.id}`))
    model.setFromAccount([cloud({ id: "a", url: "https://home.example.com" })])
    expect(model.state.fromAccount).toHaveLength(1)
    await model.saved(server(), "added")
    expect(model.state.rows).toHaveLength(1)
    expect(model.state.rows[0]).toMatchObject({ health: { version: "0.7.0" }, problem: null })
    expect(model.state.fromAccount).toEqual([])
    await model.saved(server({ name: "renamed", linked: true }), "updated")
    expect(model.state.rows).toHaveLength(1)
    expect(model.state.rows[0]?.server.name).toBe("renamed")
    expect(seen).toEqual(["added:1", "updated:1"])
  })

  test("openServerStorageFallsBackWhenTheDatabaseCannotBeOpened", async () => {
    const storage = await openServerStorage(() => Promise.reject(new Error("blocked")))
    expect(storage).toBe(unavailableStorage)
    const model = new ServersModel(storage)
    await model.load()
    expect(model.state).toMatchObject({ rows: [], loaded: true, loading: false, loadError: "The saved servers could not be read." })
  })
})

const CLOUD = "https://cloud.example.com"
/** Epoch seconds of the test clock's start. */
const START = 1_800_000_000

function cloudEntry(over: Partial<CloudServer> = {}): CloudServer {
  return { id: "srv_0123456789ab", name: "Wristcall Cloud", url: CLOUD, kind: "cloud", linked: true, agents: [], created_at: "", updated_at: "", ...over }
}

/** Route `n`: node `node-n.example.com` and token `tok-n`, valid for 300 s from `now` (ms). */
function routeReply(n: number, now: number): Response {
  return ok({ node: { id: `node-${n}`, url: `https://node-${n}.example.com`, region: "sao" }, token: `tok-${n}`, expires_at: Math.floor(now / 1000) + 300, fallback: null })
}

/** Servers with the Cloud entry: the Cloud answers the routes with `route(n)`, the nodes with `node(host, path)`. */
function cloudWorld(
  node: (host: string, path: string) => Response | null = () => ok({ agents: [] }),
  route: (n: number, now: number) => Response = routeReply,
) {
  const clock = { now: START * 1000 }
  let n = 0
  const cloudFetch = mockFetch(() => route(++n, clock.now))
  const router = new CloudRouter({ accessToken: async () => "access-token", cloudClient: new CloudClient(CLOUD, cloudFetch) }, () => clock.now)
  const hosts = ["node-1.example.com", "node-2.example.com", "node-3.example.com"]
  const { servers, fetchMock } = serversWith([], Object.fromEntries(hosts.map((host) => [host, (path: string) => node(host, path)])))
  const account = { signedIn: true }
  servers.attachRouter(router, { signedIn: () => account.signedIn, fetchImpl: fetchMock })
  return { servers, router, cloudFetch, nodeFetch: fetchMock, clock, account }
}

const headersOf = (init: RequestInit) => init.headers as Record<string, string>

describe("Wristcall Cloud entry", () => {
  test("cloud entry is kept apart from pending servers", async () => {
    const { servers, nodeFetch } = cloudWorld()
    const office = cloud()
    expect(isCloudEntry(cloudEntry())).toBe(true)
    expect(isCloudEntry(office)).toBe(false)
    servers.setFromAccount([office, cloudEntry()])
    expect(servers.state.fromAccount).toEqual([office])
    expect(servers.state.cloud).toBeNull()
    servers.setCloud(cloudEntry())
    expect(servers.state.cloud).toEqual(cloudEntry())
    expect(servers.state.fromAccount).toEqual([office])
    expect(servers.state.rows).toEqual([])
    // A self-hosted entry is never taken for the Cloud entry.
    servers.setCloud(office)
    expect(servers.state.cloud).toBeNull()
    // Reading the saved servers keeps the entry; no health check or token check runs for it.
    servers.setCloud(cloudEntry())
    await servers.load()
    expect(servers.state.cloud).toEqual(cloudEntry())
    expect(nodeFetch.calls).toHaveLength(0)
  })

  test("client('cloud') uses the route node and token", async () => {
    const { servers, cloudFetch, nodeFetch } = cloudWorld()
    servers.setCloud(cloudEntry())
    const client = await servers.client(CLOUD_ENTRY_ID)
    expect(client).toBeInstanceOf(RoutedServerClient)
    expect(await client!.agents()).toEqual([])
    expect(cloudFetch.calls).toHaveLength(1)
    expect(JSON.parse(String(cloudFetch.lastInit().body))).toEqual({ purpose: "manage" })
    expect(nodeFetch.lastUrl()).toBe("https://node-1.example.com/v1/agents")
    expect(headersOf(nodeFetch.lastInit()).Authorization).toBe("Bearer tok-1")
    // The node is not the Cloud: no client header goes there.
    expect(headersOf(nodeFetch.lastInit())["X-Wristcall-Client"]).toBeUndefined()
    // The export goes through the route as well.
    const blobWorld = cloudWorld((_host, path) => (path === "calls/export" ? new Response("# calls", { status: 200, headers: { "content-disposition": 'attachment; filename="calls.md"' } }) : null))
    blobWorld.servers.setCloud(cloudEntry())
    const exported = await (await blobWorld.servers.client(CLOUD_ENTRY_ID))!.exportCalls("md", {})
    expect(exported.filename).toBe("calls.md")
    expect(blobWorld.nodeFetch.lastUrl()).toBe("https://node-1.example.com/v1/calls/export?format=md")
    expect(headersOf(blobWorld.nodeFetch.lastInit()).Authorization).toBe("Bearer tok-1")
    // Without a router (no account) there is no Cloud entry client.
    const plain = serversWith([]).servers
    plain.setCloud(cloudEntry())
    expect(await plain.client(CLOUD_ENTRY_ID)).toBeNull()
  })

  test("client('cloud') never throws on creation; a 503 surfaces on the first request", async () => {
    const unavailable = () => new Response(JSON.stringify({ error: "no_node_available", message: "no node" }), { status: 503, headers: { "Retry-After": "30" } })
    const { servers, cloudFetch, nodeFetch } = cloudWorld(undefined, unavailable)
    servers.setCloud(cloudEntry())
    const client = await servers.client(CLOUD_ENTRY_ID)
    expect(client).not.toBeNull()
    expect(cloudFetch.calls).toHaveLength(0)
    const error = await client!.agents().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 503, code: "no_node_available" })
    expect(userMessage(error)).toBe("Wristcall Cloud is temporarily unavailable. Try again in a minute.")
    expect(nodeFetch.calls).toHaveLength(0)
  })

  test("a stored routed client keeps working after the route expires", async () => {
    const { servers, cloudFetch, nodeFetch, clock } = cloudWorld()
    servers.setCloud(cloudEntry())
    const client = (await servers.client(CLOUD_ENTRY_ID))!
    await client.agents()
    clock.now += 200_000
    await client.agents()
    expect(cloudFetch.calls).toHaveLength(1)
    expect(headersOf(nodeFetch.lastInit()).Authorization).toBe("Bearer tok-1")
    // Less than 30 s left on the token: the same client asks for a new route.
    clock.now += 71_000
    await client.agents()
    expect(cloudFetch.calls).toHaveLength(2)
    expect(nodeFetch.lastUrl()).toBe("https://node-2.example.com/v1/agents")
    expect(headersOf(nodeFetch.lastInit()).Authorization).toBe("Bearer tok-2")
  })

  test("retries once with a fresh route after a 401", async () => {
    const refused = () => new Response(JSON.stringify({ error: "unauthorized", message: "expired" }), { status: 401 })
    const w = cloudWorld((host) => (host === "node-1.example.com" ? refused() : ok({ id: "ag_1", slug: "x" })))
    w.servers.setCloud(cloudEntry())
    const client = (await w.servers.client(CLOUD_ENTRY_ID))!
    expect(await client.createAgent({ slug: "x" })).toBeDefined()
    expect(w.cloudFetch.calls).toHaveLength(2)
    expect(w.nodeFetch.calls.map((call) => `${call.init.method} ${call.url}`)).toEqual([
      "POST https://node-1.example.com/v1/agents",
      "POST https://node-2.example.com/v1/agents",
    ])
    expect(headersOf(w.nodeFetch.lastInit()).Authorization).toBe("Bearer tok-2")
    // Only once: a node that keeps refusing gives the 401 after the second try.
    const always = cloudWorld(() => refused())
    always.servers.setCloud(cloudEntry())
    const stubborn = (await always.servers.client(CLOUD_ENTRY_ID))!
    await expect(stubborn.agents()).rejects.toMatchObject({ status: 401 })
    expect(always.cloudFetch.calls).toHaveLength(2)
    expect(always.nodeFetch.calls).toHaveLength(2)
  })

  test("client('cloud') works before the agenda is read: the first request goes through the route", async () => {
    // A reload on /servers/cloud/agents asks for the client before the agenda read sets `state.cloud`.
    const { servers, cloudFetch, nodeFetch } = cloudWorld()
    expect(servers.state.cloud).toBeNull()
    const client = await servers.client(CLOUD_ENTRY_ID)
    expect(client).toBeInstanceOf(RoutedServerClient)
    expect(cloudFetch.calls).toHaveLength(0)
    expect(await client!.agents()).toEqual([])
    expect(cloudFetch.calls).toHaveLength(1)
    expect(nodeFetch.lastUrl()).toBe("https://node-1.example.com/v1/agents")
    // A refusal of the route reaches the screen with its own text.
    const noPlan = cloudWorld(undefined, () => new Response(JSON.stringify({ error: "plan_required", message: "no plan" }), { status: 402 }))
    const refused = await (await noPlan.servers.client(CLOUD_ENTRY_ID))!.agents().catch((e: unknown) => e)
    expect(userMessage(refused)).toBe("This account has no Wristcall Cloud plan.")
    // Signed out: no client.
    const out = cloudWorld()
    out.account.signedIn = false
    expect(await out.servers.client(CLOUD_ENTRY_ID)).toBeNull()
  })

  test("sign out invalidates the route and clears the cloud entry", async () => {
    const { servers, router, cloudFetch, account } = cloudWorld()
    servers.setCloud(cloudEntry())
    await (await servers.client(CLOUD_ENTRY_ID))!.agents()
    expect(cloudFetch.calls).toHaveLength(1)
    account.signedIn = false
    await servers.forgetAccount()
    expect(servers.state.cloud).toBeNull()
    expect(await servers.client(CLOUD_ENTRY_ID)).toBeNull()
    // The route of the account that left is gone: the next one is asked anew.
    expect((await router.manage()).token).toBe("tok-2")
    expect(cloudFetch.calls).toHaveLength(2)
  })
})
