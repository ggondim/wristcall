import type { ServerChange } from "../models/servers"

/**
 * Asks the browser not to evict this site's data (IndexedDB holds the servers and their tokens) once the first server is
 * saved: until then there is nothing to lose. Best effort, once per page session; the answer and any error are ignored
 * (a browser may refuse, or have no such call).
 */
export function persistOnFirstServer(servers: { onChange(listener: (event: ServerChange) => void): () => void }): () => void {
  let asked = false
  const stop = servers.onChange((event) => {
    if (asked || event.kind !== "added") return
    asked = true
    try {
      void Promise.resolve(globalThis.navigator?.storage?.persist?.()).catch(() => undefined)
    } catch {
      // Not available or refused: nothing to do.
    }
  })
  return stop
}

/**
 * On opening: if servers are saved and the browser has not granted persistent storage, asks again. The first-add request
 * can be refused (Chrome decides by heuristic, such as the app being installed), and nothing else would ever ask again.
 * Best effort; every error is ignored.
 */
export async function persistWhenServersExist(storage: { list(): Promise<readonly unknown[]> }): Promise<void> {
  try {
    const manager = globalThis.navigator?.storage
    if (typeof manager?.persist !== "function" || typeof manager.persisted !== "function") return
    if ((await manager.persisted()) !== false) return
    if ((await storage.list()).length === 0) return
    await manager.persist()
  } catch {
    // Not available, refused, or the database could not be read: nothing to do.
  }
}
