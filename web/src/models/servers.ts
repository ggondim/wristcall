import { canonical, parseServerAddress, sameUrl } from "../lib/address"
import { ApiError, userMessage } from "../lib/errors"
import { newId } from "../lib/ids"
import { ServerClient, type Health } from "../api/server"
import type { CloudServer } from "../api/cloud"
import { RoutedServerClient, type ManageRouter } from "../api/routedServer"
import type { SavedServer } from "../store/servers"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { BaseModel } from "../ui/useModel"

/** What the list needs from the store (the real `ServerStore`, or a stand-in in a test). */
export type ServerStorage = Pick<ServerStore, "list" | "get" | "put" | "remove" | "update">

/** What linking to the account needs from the sign-in: the Cloud's address and a per-server token (the real `AccountSession`). */
export interface AccountLink {
  readonly state: "signedOut" | "signedIn"
  readonly cloudUrl: string
  serverToken(audience: string): Promise<{ token: string; audience: string; expires_at: number }>
  /** Calls the listener when `state` changes (signing in or out, even in the background). Returns the unsubscribe. */
  onStateChange?(listener: () => void): () => void
}

export interface ServerRow {
  server: SavedServer
  /** Null until the server answered (or when it did not). */
  health: Health | null
  /** Short text for the screen: unreachable or token refused. Null when nothing is wrong (or not checked yet). */
  problem: string | null
}

export interface ServersState {
  rows: ServerRow[]
  /** Servers of the account's agenda that this browser has no token for (never the Wristcall Cloud entry). */
  fromAccount: CloudServer[]
  /** The Wristcall Cloud entry of the account's agenda: managed through the Cloud route, never saved here. */
  cloud: CloudServer | null
  loading: boolean
  /** True once the first read of the database finished. */
  loaded: boolean
  loadError: string | null
}

export type ServerChange = { kind: "added" | "updated" | "removed"; server: SavedServer }

/** The id `client()` takes for the Wristcall Cloud entry (a saved server's id is a UUID). */
export const CLOUD_ENTRY_ID = "cloud"

/** What the screens call the Wristcall Cloud entry (its section, the history's server filter), whatever the agenda names it. */
export const CLOUD_ENTRY_NAME = "Wristcall Cloud"

/** The Cloud's own entry in the agenda (`kind: "cloud"`): the Cloud manages it, no app writes it. */
export function isCloudEntry(server: CloudServer): boolean {
  return server.kind === "cloud"
}

// Same texts as the iPhone app.
export const UNREACHABLE = "Can't reach host."
export const TOKEN_REFUSED = "Token was revoked."
export const NAME_EMPTY = "The name cannot be empty."
export const NAME_TOO_LONG = "The name can have at most 64 characters."
export const NAME_INVALID = "The name cannot have control characters."
export const BAD_ADDRESS = "Use an https:// address (http:// only for localhost)."
export const ALREADY_ADDED = "This server is already added."
export const OTHER_ACCOUNT = "This server uses another account service."
export const NO_ACCOUNT = "This server has no account set up."
export const WRONG_AUDIENCE = "The account service named a different server."
export const ALREADY_LINKED = "Your account is linked to another user of this server."
export const SIGN_IN_FIRST = "Sign in to your wristcall account first."
export const MALFORMED_REPLY = "The server's reply was not understood."

const MAX_NAME = 64
const LOAD_FAILED = "The saved servers could not be read."

/** Stand-in when the database cannot be opened: the list shows the failure, and nothing is saved or removed. */
export const unavailableStorage: ServerStorage = {
  list: () => Promise.reject(new Error(LOAD_FAILED)),
  get: async () => null,
  put: () => Promise.reject(new Error(LOAD_FAILED)),
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

/** Throws an `Error` whose message is the text for the screen. */
export function checkName(clean: string): void {
  if (clean === "") throw new Error(NAME_EMPTY)
  if (/\p{Cc}/u.test(clean)) throw new Error(NAME_INVALID)
  if (Array.from(clean).length > MAX_NAME) throw new Error(NAME_TOO_LONG)
}

/**
 * The per-server token for linking, after checking that the server uses this Cloud as its account service. The server's
 * `health.account.issuer` is the Cloud's own address (the Cloud signs the per-server tokens as itself), not the identity
 * provider's. The token must also name this very server. Throws an `Error` whose message is the text for the screen.
 */
export async function accountServerToken(url: URL, health: Health, link: AccountLink): Promise<string> {
  if (health.account == null) throw new Error(NO_ACCOUNT)
  if (!sameUrl(health.account.issuer, link.cloudUrl)) throw new Error(OTHER_ACCOUNT)
  const audience = canonical(url)
  let reply: { token: string; audience: string }
  try {
    reply = await link.serverToken(audience)
  } catch (e) {
    throw new Error(userMessage(e))
  }
  if (typeof reply.token !== "string" || reply.token === "" || !sameUrl(reply.audience, audience)) {
    throw new Error(WRONG_AUDIENCE)
  }
  return reply.token
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
  #router: ManageRouter | null = null
  #routedFetch: typeof fetch | undefined
  #signedIn: () => boolean = () => true
  #beforeRemove: (server: SavedServer) => Promise<void> = async () => {}
  #generation = 0
  readonly #cleanups = new Set<Promise<void>>()
  /** Counts the changes made here (rename, remove, saved): a read of the database that began before one is stale. */
  #changes = 0

  constructor(store: ServerStorage, serverClientFor: (server: SavedServer) => ServerClient = defaultServerClient) {
    super({ rows: [], fromAccount: [], cloud: null, loading: false, loaded: false, loadError: null })
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

  /**
   * The management client of a saved server, or null when it is not (any longer) saved. For `CLOUD_ENTRY_ID`, the routed
   * client of the Wristcall Cloud entry while the account is signed in (and has a router), even before the agenda read
   * finished (a reload on the entry's screens): made without reaching the Cloud, it asks for the route on every request,
   * so a missing plan or node surfaces on the first one (`402`, `404`, `503`) with its own text.
   */
  async client(id: string): Promise<ServerClient | null> {
    if (id === CLOUD_ENTRY_ID) {
      return this.#router === null || !this.#signedIn() ? null : new RoutedServerClient(this.#router, this.#routedFetch)
    }
    const server = await this.#store.get(id)
    return server === null ? null : this.#clientFor(server)
  }

  /** Throws an `Error` whose message is the text for the screen. */
  async rename(id: string, name: string): Promise<void> {
    const clean = name.trim()
    checkName(clean)
    const server = await this.#store.update(id, { name: clean })
    this.#changes++
    if (server === null) return
    this.set({ rows: this.state.rows.map((row) => (row.server.id === id ? { ...row, server } : row)) })
    this.#emit({ kind: "updated", server })
  }

  /** Whether a server with this address (compared in canonical form) is already saved. */
  async has(url: string): Promise<boolean> {
    return (await this.#store.list()).some((server) => sameUrl(server.url, url))
  }

  /** Saves a new server and checks it. Throws an `Error` whose message is the text for the screen. */
  async add(fields: Omit<SavedServer, "id" | "addedAt">): Promise<SavedServer> {
    const parsed = parseServerAddress(fields.url)
    if (parsed === null) throw new Error(BAD_ADDRESS)
    if (await this.has(fields.url)) throw new Error(ALREADY_ADDED)
    const server: SavedServer = { ...fields, url: canonical(parsed), id: newId(), addedAt: Date.now() }
    await this.#store.put(server)
    // The check (health and providers) can take its time: the caller does not wait for it.
    void this.saved(server, "added")
    return server
  }

  /**
   * Forgets the server here (same order as the iPhone app): it is deleted locally first, and that is all the caller
   * waits for. Then, each on its own and best effort, its push key is deleted on the server and the hook undoes the
   * rest (the relay registration). The token keeps working until it is revoked on the server.
   */
  async remove(id: string): Promise<void> {
    const server = await this.#store.get(id)
    if (server === null) return
    await this.#store.remove(id)
    this.#changes++
    const rows = this.state.rows.filter((row) => row.server.id !== id)
    this.set({ rows, fromAccount: this.#pending(rows) })
    this.#emit({ kind: "removed", server })
    const cleanup = this.#cleanUp(server).finally(() => this.#cleanups.delete(cleanup))
    this.#cleanups.add(cleanup)
  }

  /** Resolves when the cleanups started by `remove` are over (they never reject). */
  async cleanupsDone(): Promise<void> {
    while (this.#cleanups.size > 0) await Promise.all([...this.#cleanups])
  }

  async #cleanUp(server: SavedServer): Promise<void> {
    if (server.pushKey !== null) {
      try {
        await this.#clientFor(server).clearPushKey()
      } catch {
        // The server may be gone or unreachable: it is forgotten here anyway.
      }
    }
    try {
      await this.#beforeRemove(server)
    } catch {
      // Same: a failure at the relay changes nothing here.
    }
  }

  /**
   * Links the account to a saved server (the user of its personal token). Throws an `Error` whose message is the text
   * for the screen; a `409` (the account is linked to another user there) is never counted as linked.
   */
  async linkAccount(id: string, session: AccountLink): Promise<void> {
    if (session.state !== "signedIn") throw new Error(SIGN_IN_FIRST)
    const server = await this.#store.get(id)
    if (server === null) throw new Error("This server is no longer saved.")
    const client = this.#clientFor(server)
    let serverToken: string
    try {
      const health = await client.health()
      serverToken = await accountServerToken(new URL(server.url), health, session)
    } catch (e) {
      throw new Error(userMessage(e))
    }
    try {
      const reply = await client.linkWithToken(serverToken)
      if (reply.linked !== true) throw new Error(MALFORMED_REPLY)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) throw new Error(ALREADY_LINKED)
      throw new Error(userMessage(e))
    }
    const updated = await this.#store.update(id, { linked: true })
    this.#changes++
    if (updated === null) return
    this.set({ rows: this.state.rows.map((row) => (row.server.id === id ? { ...row, server: updated } : row)) })
    this.#emit({ kind: "updated", server: updated })
  }

  /**
   * Before the account is deleted: each linked server drops its account link (`DELETE /v1/account/link`), best effort
   * (a server that is down, or has no token here, keeps its link until the user unlinks it there).
   */
  async unlinkAccounts(): Promise<void> {
    let servers: SavedServer[]
    try {
      servers = await this.#store.list()
    } catch {
      return
    }
    await Promise.allSettled(servers.filter((server) => server.linked).map(async (server) => this.#clientFor(server).unlinkAccount()))
  }

  /**
   * After signing out: the agenda ids and the links belonged to that account, and its list is no longer shown. The
   * servers and their tokens stay. No change event: nobody mirrors this to the (gone) agenda.
   */
  async forgetAccount(): Promise<void> {
    this.#account = []
    this.#router?.invalidate()
    this.set({ cloud: null })
    for (const server of await this.#store.list()) {
      if (server.linked || server.cloudServerId !== null) await this.#store.update(server.id, { linked: false, cloudServerId: null })
    }
    this.#changes++
    const stored = new Map((await this.#store.list()).map((server) => [server.id, server]))
    this.set({ rows: this.state.rows.map((row) => ({ ...row, server: stored.get(row.server.id) ?? row.server })), fromAccount: [] })
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
    this.#account = servers.filter((server) => !isCloudEntry(server))
    this.set({ fromAccount: this.#pending(this.state.rows) })
  }

  /** The Wristcall Cloud entry the agenda holds now (null: none, or signed out). Only a `kind: "cloud"` entry is taken. */
  setCloud(entry: CloudServer | null): void {
    this.set({ cloud: entry !== null && isCloudEntry(entry) ? entry : null })
  }

  /**
   * The account's router: the Cloud entry's client asks it for the node on every request. `signedIn` tells whether the
   * account is signed in (no client otherwise); `fetchImpl` is for tests.
   */
  attachRouter(router: ManageRouter, opts: { signedIn?: () => boolean; fetchImpl?: typeof fetch } = {}): void {
    this.#router = router
    this.#signedIn = opts.signedIn ?? (() => true)
    this.#routedFetch = opts.fetchImpl
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
