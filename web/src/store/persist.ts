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
