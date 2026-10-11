import { afterEach, describe, expect, test, vi } from "vitest"
import type { ServerChange } from "../models/servers"
import { persistOnFirstServer } from "./persist"

const server = { id: "a", name: "A", url: "https://a.example", token: "t", addedAt: 1 } as ServerChange["server"]

function fakeServers() {
  const listeners = new Set<(event: ServerChange) => void>()
  return {
    onChange(listener: (event: ServerChange) => void) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    emit(event: ServerChange) {
      for (const listener of listeners) listener(event)
    },
  }
}

function stubStorage(storage: unknown): void {
  Object.defineProperty(globalThis.navigator, "storage", { value: storage, configurable: true })
}

describe("persistOnFirstServer", () => {
  afterEach(() => {
    Reflect.deleteProperty(globalThis.navigator, "storage")
  })

  test("asks once, on the first added server", () => {
    const persist = vi.fn().mockResolvedValue(true)
    stubStorage({ persist })
    const servers = fakeServers()
    persistOnFirstServer(servers)
    servers.emit({ kind: "updated", server })
    servers.emit({ kind: "removed", server })
    expect(persist).not.toHaveBeenCalled()
    servers.emit({ kind: "added", server })
    servers.emit({ kind: "added", server: { ...server, id: "b" } })
    expect(persist).toHaveBeenCalledTimes(1)
  })

  test("does nothing, and does not throw, where the browser has no persist", () => {
    stubStorage({})
    const servers = fakeServers()
    persistOnFirstServer(servers)
    expect(() => servers.emit({ kind: "added", server })).not.toThrow()
  })

  test("ignores a refusal and an error", async () => {
    const persist = vi.fn().mockRejectedValue(new Error("no"))
    stubStorage({ persist })
    const servers = fakeServers()
    persistOnFirstServer(servers)
    servers.emit({ kind: "added", server })
    await Promise.resolve()
    expect(persist).toHaveBeenCalledTimes(1)
    stubStorage({ persist: () => { throw new Error("sync") } })
    const other = fakeServers()
    persistOnFirstServer(other)
    expect(() => other.emit({ kind: "added", server })).not.toThrow()
  })
})
