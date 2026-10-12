import { KV, SERVERS } from "./db"

export interface SavedServer {
  id: string
  name: string
  url: string
  /** Personal token (`wc_pat_`). */
  token: string
  linked: boolean
  cloudServerId: string | null
  pushKey: string | null
  pushEndpoint: string | null
  addedAt: number
}

function done<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(new Error("Local storage failed."))
  })
}

function finished(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(new Error("Local storage failed."))
    tx.onabort = () => reject(new Error("Local storage failed."))
  })
}

/** Servers and small settings in IndexedDB. Nothing is cached here: every call reads the database, so two tabs agree. */
export class ServerStore {
  constructor(private readonly db: IDBDatabase) {}

  async list(): Promise<SavedServer[]> {
    const all = await done(this.db.transaction(SERVERS, "readonly").objectStore(SERVERS).getAll() as IDBRequest<SavedServer[]>)
    return all.sort((a, b) => a.addedAt - b.addedAt)
  }

  async get(id: string): Promise<SavedServer | null> {
    const found = await done(this.db.transaction(SERVERS, "readonly").objectStore(SERVERS).get(id) as IDBRequest<SavedServer | undefined>)
    return found ?? null
  }

  async put(server: SavedServer): Promise<void> {
    const tx = this.db.transaction(SERVERS, "readwrite")
    tx.objectStore(SERVERS).put(server)
    await finished(tx)
  }

  /** Reads and writes in one transaction, so a concurrent update of another field is not lost. Null when absent. */
  async update(id: string, patch: Partial<Omit<SavedServer, "id">>): Promise<SavedServer | null> {
    const tx = this.db.transaction(SERVERS, "readwrite")
    const store = tx.objectStore(SERVERS)
    let result: SavedServer | null = null
    // The write is issued from the read's own callback: the transaction is surely still active there.
    const read = store.get(id) as IDBRequest<SavedServer | undefined>
    read.onsuccess = () => {
      if (read.result === undefined) return
      // An undefined value means "no change", not "erase the field".
      const changes = Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== undefined))
      result = { ...read.result, ...changes, id }
      store.put(result)
    }
    await finished(tx)
    return result
  }

  async remove(id: string): Promise<void> {
    const tx = this.db.transaction(SERVERS, "readwrite")
    tx.objectStore(SERVERS).delete(id)
    await finished(tx)
  }

  async getKV<T>(key: string): Promise<T | null> {
    const found = await done(this.db.transaction(KV, "readonly").objectStore(KV).get(key) as IDBRequest<T | undefined>)
    return found ?? null
  }

  async setKV(key: string, value: unknown): Promise<void> {
    const tx = this.db.transaction(KV, "readwrite")
    tx.objectStore(KV).put(value, key)
    await finished(tx)
  }

  /** Reads a value and deletes it in one transaction: of several tabs, only one gets it. */
  async takeKV<T>(key: string): Promise<T | null> {
    const tx = this.db.transaction(KV, "readwrite")
    const store = tx.objectStore(KV)
    let taken: T | null = null
    const read = store.get(key) as IDBRequest<T | undefined>
    read.onsuccess = () => {
      if (read.result === undefined) return
      taken = read.result
      store.delete(key)
    }
    await finished(tx)
    return taken
  }

  async deleteKV(key: string): Promise<void> {
    const tx = this.db.transaction(KV, "readwrite")
    tx.objectStore(KV).delete(key)
    await finished(tx)
  }
}
