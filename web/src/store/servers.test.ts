import { describe, expect, test } from "vitest"
import { openDb } from "./db"
import { type SavedServer, ServerStore } from "./servers"

function server(patch: Partial<SavedServer> & { id: string }): SavedServer {
  return { name: "s", url: "https://s.example", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...patch }
}

const fresh = async () => new ServerStore(await openDb("t-" + crypto.randomUUID()))

describe("ServerStore", () => {
  test("store: writes are read through", async () => {
    const db = await openDb("t-" + crypto.randomUUID())
    const a = new ServerStore(db)
    const b = new ServerStore(db)
    await a.put(server({ id: "1", name: "home" }))
    await b.update("1", { name: "office" })
    expect((await a.get("1"))?.name).toBe("office")
  })

  test("store: two tabs on one database see each other's writes", async () => {
    const name = "t-" + crypto.randomUUID()
    const a = new ServerStore(await openDb(name))
    const b = new ServerStore(await openDb(name))
    await a.put(server({ id: "1" }))
    await Promise.all([a.update("1", { pushKey: "wc_push_k" }), b.update("1", { linked: true })])
    expect(await b.get("1")).toMatchObject({ pushKey: "wc_push_k", linked: true })
  })

  test("storeUpdateMissingReturnsNull", async () => {
    const store = await fresh()
    expect(await store.update("nope", { name: "x" })).toBeNull()
    expect(await store.list()).toEqual([])
  })

  test("store: update keeps the id and returns the new record", async () => {
    const store = await fresh()
    await store.put(server({ id: "1" }))
    const next = await store.update("1", { name: "n", id: "other" } as Partial<SavedServer>)
    expect(next?.id).toBe("1")
    expect(next?.name).toBe("n")
    expect(await store.list()).toHaveLength(1)
  })

  test("store: list is ordered by addedAt, remove deletes", async () => {
    const store = await fresh()
    await store.put(server({ id: "b", addedAt: 20 }))
    await store.put(server({ id: "a", addedAt: 10 }))
    await store.put(server({ id: "c", addedAt: 30 }))
    expect((await store.list()).map((s) => s.id)).toEqual(["a", "b", "c"])
    await store.remove("b")
    expect(await store.get("b")).toBeNull()
    expect((await store.list()).map((s) => s.id)).toEqual(["a", "c"])
  })

  test("store: key/value", async () => {
    const store = await fresh()
    expect(await store.getKV("session")).toBeNull()
    await store.setKV("session", { refresh: "r" })
    expect(await store.getKV("session")).toEqual({ refresh: "r" })
    await store.deleteKV("session")
    expect(await store.getKV("session")).toBeNull()
  })
})
