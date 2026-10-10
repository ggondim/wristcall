export const SERVERS = "servers"
export const KV = "kv"

/** IndexedDB "wristcall" v1: `servers` (keyed by `id`) and `kv` (out of line keys). */
export function openDb(name = "wristcall"): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(name, 1)
    open.onupgradeneeded = () => {
      const db = open.result
      if (!db.objectStoreNames.contains(SERVERS)) db.createObjectStore(SERVERS, { keyPath: "id" })
      if (!db.objectStoreNames.contains(KV)) db.createObjectStore(KV)
    }
    open.onsuccess = () => {
      const db = open.result
      // Another tab upgrading the schema must not wait for this one.
      db.onversionchange = () => db.close()
      resolve(db)
    }
    open.onerror = () => reject(new Error("Could not open the local storage."))
    open.onblocked = () => reject(new Error("Local storage is blocked by another tab."))
  })
}
