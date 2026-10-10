import { sameUrl } from "../lib/address"
import { ApiError } from "../lib/errors"
import { ServerClient, type Health } from "../api/server"
import type { CloudServer } from "../api/cloud"
import type { SavedServer } from "../store/servers"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { BaseModel } from "../ui/useModel"

/** What the list needs from the store (the real `ServerStore`, or a stand-in in a test). */
export type ServerStorage = Pick<ServerStore, "list" | "get" | "remove" | "update">

export interface ServerRow {
  server: SavedServer
  /** Null until the server answered (or when it did not). */
  health: Health | null
  /** Short text for the screen: unreachable or token refused. Null when nothing is wrong (or not checked yet). */
  problem: string | null
}

export interface ServersState {
  rows: ServerRow[]
  /** Servers of the account's agenda that this browser has no token for. */
  fromAccount: CloudServer[]
  loading: boolean
  /** True once the first read of the database finished. */
  loaded: boolean
  loadError: string | null
}

export type ServerChange = { kind: "added" | "updated" | "removed"; server: SavedServer }

// Same texts as the iPhone app.
export const UNREACHABLE = "Can't reach host."
export const TOKEN_REFUSED = "Token was revoked."
export const NAME_EMPTY = "The name cannot be empty."
export const NAME_TOO_LONG = "The name can have at most 64 characters."
export const NAME_INVALID = "The name cannot have control characters."

const MAX_NAME = 64
const LOAD_FAILED = "The saved servers could not be read."

/** Stand-in when the database cannot be opened: the list shows the failure, and nothing is saved or removed. */
export const unavailableStorage: ServerStorage = {
  list: () => Promise.reject(new Error(LOAD_FAILED)),
  get: async () => null,
  remove: () => Promise.reject(new Error(LOAD_FAILED)),
  update: () => Promise.reject(new Error(LOAD_FAILED)),
}

/** The store over IndexedDB; when that cannot be opened (blocked, private mode), the stand-in, so every screen still renders. */
export async function openServerStorage(open: () => Promise<IDBDatabase> = openDb): Promise<ServerStorage> {
  try {
    return new ServerStore(await open())
  } catch {
    return unavailableStorage
  }
}

/** The client of one saved server, with its token. */
export function defaultServerClient(server: SavedServer): ServerClient {
  return new ServerClient(new URL(server.url), server.token)
}

/** Health tells whether the host answers; `providers` (needs the token) tells whether the token still works. */
async function check(makeClient: () => ServerClient): Promise<Pick<ServerRow, "health" | "problem">> {
  let client: ServerClient
  let health: Health
  try {
    client = makeClient() // a stored address that no longer parses fails here
    health = await client.health()
  } catch {
    return { health: null, problem: UNREACHABLE }
  }
  try {
    await client.providers()
    return { health, problem: null }
  } catch (e) {
    if (e instanceof ApiError && (e.status === 401 || e.status === 403)) return { health, problem: TOKEN_REFUSED }
    return { health, problem: UNREACHABLE }
  }
}

/** The saved servers, how each one is doing, and the account's servers that are still to be set up here. */
export class ServersModel extends BaseModel<ServersState> {
  readonly #store: ServerStorage
  readonly #clientFor: (server: SavedServer) => ServerClient
  readonly #listeners = new Set<(event: ServerChange) => void>()
  #account: CloudServer[] = []
  #beforeRemove: (server: SavedServer) => Promise<void> = async () => {}
  #generation = 0
  /** Counts the changes made here (rename, remove, saved): a read of the database that began before one is stale. */
  #changes = 0

  constructor(store: ServerStorage, serverClientFor: (server: SavedServer) => ServerClient = defaultServerClient) {
    super({ rows: [], fromAccount: [], loading: false, loaded: false, loadError: null })
    this.#store = store
    this.#clientFor = serverClientFor
  }

  /** Reads the database, then checks every server at once. A server that fails becomes a `problem`, not an exception. */
  async load(): Promise<void> {
    const generation = ++this.#generation
    this.set({ loading: true })
    let servers: SavedServer[]
    try {
      let changes: number
      do {
        changes = this.#changes
        servers = await this.#store.list()
        if (generation !== this.#generation) return
      } while (changes !== this.#changes) // a rename or removal finished meanwhile: read again
    } catch {
      if (generation !== this.#generation) return
      this.set({ rows: [], loading: false, loaded: true, loadError: LOAD_FAILED, fromAccount: this.#pending([]) })
      return
    }
    if (generation !== this.#generation) return
    // What is already known stays on screen while the servers are asked again.
    const known = new Map(this.state.rows.map((row) => [row.server.id, row]))
    const rows = servers.map((server): ServerRow => {
      const before = known.get(server.id)
      return { server, health: before?.health ?? null, problem: before?.problem ?? null }
    })
    this.set({ rows, loaded: true, loadError: null, fromAccount: this.#pending(rows) })
    await Promise.all(
      servers.map(async (server) => {
        const result = await check(() => this.#clientFor(server))
        if (generation !== this.#generation) return
        this.set({ rows: this.state.rows.map((row) => (row.server.id === server.id ? { ...row, ...result } : row)) })
      }),
    )
    if (generation === this.#generation) this.set({ loading: false })
  }

  /** The management client of a saved server, or null when it is not (any longer) saved. */
  async client(id: string): Promise<ServerClient | null> {
    const server = await this.#store.get(id)
    return server === null ? null : this.#clientFor(server)
  }

  /** Throws an `Error` whose message is the text for the screen. */
  async rename(id: string, name: string): Promise<void> {
    const clean = name.trim()
    if (clean === "") throw new Error(NAME_EMPTY)
    if (/\p{Cc}/u.test(clean)) throw new Error(NAME_INVALID)
    if (Array.from(clean).length > MAX_NAME) throw new Error(NAME_TOO_LONG)
    const server = await this.#store.update(id, { name: clean })
    this.#changes++
    if (server === null) return
    this.set({ rows: this.state.rows.map((row) => (row.server.id === id ? { ...row, server } : row)) })
    this.#emit({ kind: "updated", server })
  }

  /** Forgets the server here. The token keeps working until it is revoked on the server. */
  async remove(id: string): Promise<void> {
    const server = await this.#store.get(id)
    if (server === null) return
    // Whatever has to be undone with the server first (the push key); a failure there does not keep the server.
    try {
      await this.#beforeRemove(server)
    } catch {
      // The server may be gone or unreachable: removing it here is still what was asked.
    }
    await this.#store.remove(id)
    this.#changes++
    const rows = this.state.rows.filter((row) => row.server.id !== id)
    this.set({ rows, fromAccount: this.#pending(rows) })
    this.#emit({ kind: "removed", server })
  }

  /** A server was saved by another screen (adding, or the account link): checks it and tells the listeners. */
  async saved(server: SavedServer, kind: "added" | "updated"): Promise<void> {
    this.#changes++
    const rows = this.state.rows.some((row) => row.server.id === server.id)
      ? this.state.rows.map((row) => (row.server.id === server.id ? { ...row, server } : row))
      : [...this.state.rows, { server, health: null, problem: null }]
    this.set({ rows, fromAccount: this.#pending(rows) })
    this.#emit({ kind, server })
    const result = await check(() => this.#clientFor(server))
    this.set({ rows: this.state.rows.map((row) => (row.server.id === server.id ? { ...row, ...result } : row)) })
  }

  /** The account's agenda; the servers that are already saved here are left out. */
  setFromAccount(servers: CloudServer[]): void {
    this.#account = servers
    this.set({ fromAccount: this.#pending(this.state.rows) })
  }

  /** Runs before a server is deleted (the push key is unregistered there). Task 10 sets it. */
  setBeforeRemove(hook: (server: SavedServer) => Promise<void>): void {
    this.#beforeRemove = hook
  }

  /** Calls `listener` for every server added, changed or removed. Returns the unsubscribe. */
  onChange(listener: (event: ServerChange) => void): () => void {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }

  #pending(rows: ServerRow[]): CloudServer[] {
    return this.#account.filter((entry) => !rows.some((row) => sameUrl(row.server.url, entry.url)))
  }

  #emit(event: ServerChange): void {
    for (const listener of [...this.#listeners]) {
      try {
        listener(event)
      } catch {
        // One listener's failure does not reach the others, nor the screen that caused the change.
      }
    }
  }
}
