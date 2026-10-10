import { describe, expect, test } from "vitest"
import { ApiError } from "../lib/errors"
import { ServerClient } from "../api/server"
import type { CloudServer } from "../api/cloud"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { openServerStorage, ServersModel, unavailableStorage } from "./servers"

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

  test("removeCallsHookThenForgetsAndNotifies", async () => {
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
    expect(order).toEqual(["hook:1:present", "removed:1"])
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

