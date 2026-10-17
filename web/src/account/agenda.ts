import type { CloudAgent, CloudServer } from "../api/cloud"
import { OidcError } from "../api/oidc"
import type { AgentDetail } from "../api/server"
import { AGENT_COLORS } from "../ui/agentColor"
import { ApiError } from "../lib/errors"
import { parseServerAddress, sameUrl } from "../lib/address"
import type { AgendaControl } from "../models/account"
import type { ServerChange, ServersModel, ServerStorage } from "../models/servers"
import type { AccountSession } from "./session"

/** What the mirror needs from the sign-in: the state, a fresh access token and the Cloud client. */
export type AgendaSession = Pick<AccountSession, "state" | "accessToken" | "accessTokenRefused" | "cloudClient">

/** What the mirror needs from the servers list: a client per saved server (to read its agents). */
export type AgendaServers = Pick<ServersModel, "client">

/** What the mirror needs from the store: it reads the latest saved servers and keeps the agenda id. */
export type AgendaStore = Pick<ServerStorage, "list" | "get" | "update">

const WRITE_NOTICE = "Can't update your servers in wristcall Cloud."
const READ_NOTICE = "Can't read your servers from wristcall Cloud."
const ID_SHAPE = /^[A-Za-z0-9_-]+$/
const ICON_SHAPE = /^[a-z0-9.]+$/
const CALL_TYPES = new Set(["conversation", "one-shot", "monologue"])

const segmenter = typeof Intl !== "undefined" && "Segmenter" in Intl ? new Intl.Segmenter() : null

/** A job of an older generation (signed out or the account deleted meanwhile): it writes nothing. */
class Stale extends Error {}

type Outcome = "done" | "failed" | "stale" | "signedOut"

function isSignedOut(error: unknown): boolean {
  return error instanceof OidcError && error.kind === "signed_out"
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

function characters(text: string): string[] {
  return segmenter === null ? Array.from(text) : Array.from(segmenter.segment(text), (part) => part.segment)
}

function cutText(text: string, limit: number): string {
  let result = ""
  let count = 0
  for (const character of characters(text.replace(/\p{Cc}/gu, "").trim())) {
    const size = Array.from(character).length
    if (count + size > limit) break
    result += character
    count += size
  }
  return result.trimEnd()
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return ""
  }
}

/**
 * Mirrors this browser's servers (name, canonical URL, `linked`) and their agents to the account's agenda in wristcall
 * Cloud, which the watch reads when it signs in with the account. Port of the iPhone's `AgendaSync`.
 *
 * Best effort, in the background: every change only queues a job and returns at once, so a slow or hung Cloud never holds
 * up managing servers or agents. Jobs run one after another; each reads the latest state when it runs (several changes of
 * one server's agents send only the last list). An error only sets `notice`. `invalidate()` (sign out, account deletion)
 * drops the queue: a job still running checks the generation before every Cloud write, so nothing is written after it.
 */
export class AgendaSync implements AgendaControl {
  /** The Cloud keeps at most this many agents per server. */
  static readonly agentLimit = 50
  /** The Cloud's limit for names (Unicode scalars). */
  static readonly textLimit = 64

  readonly #session: AgendaSession
  readonly #servers: AgendaServers
  readonly #store: AgendaStore
  readonly #noticeListeners = new Set<() => void>()
  #notice: string | null = null
  /** The access token of the last Cloud call: a `401` to it makes the session renew (or sign out). */
  #lastToken: string | null = null
  /** Bumped by `invalidate()`; a job of an older generation writes nothing. */
  #generation = 0
  /** The last job queued; each job waits for the one before it. */
  #chain: Promise<void> = Promise.resolve()
  /** The agents lists not sent yet, by local server id: a job takes the latest one when it gets to it. */
  readonly #pendingAgents = new Map<string, AgentDetail[]>()
  /** The servers with an add/update job queued and not started yet. */
  readonly #queuedServers = new Set<string>()
  #pushAllQueued = false
  /** While set (a deletion or a sign out on its way), changes queue nothing. */
  #suspended = false

  constructor(session: AgendaSession, servers: AgendaServers, store: AgendaStore) {
    this.#session = session
    this.#servers = servers
    this.#store = store
  }

  /** The last failure, for the screen; cleared by the next success. */
  get notice(): string | null {
    return this.#notice
  }

  onNoticeChange(listener: () => void): () => void {
    this.#noticeListeners.add(listener)
    return () => {
      this.#noticeListeners.delete(listener)
    }
  }

  // Changes (queue a job, return at once)

  /**
   * Adds or updates every server, keeps the agenda ids, then sends each one's agents (a server that does not answer keeps
   * the agents the agenda has).
   */
  pushAll(): void {
    if (!this.#active() || this.#pushAllQueued) return
    this.#pushAllQueued = true
    this.#enqueue(async (generation) => {
      this.#pushAllQueued = false
      let failed = false
      for (const saved of await this.#store.list()) {
        const outcome = await this.#attempt(generation, async () => {
          const id = await this.#upsert(saved.id, generation)
          if (id === null) return
          let agents: AgentDetail[]
          try {
            const client = await this.#servers.client(saved.id)
            if (client === null) return
            agents = await client.agents()
          } catch {
            return // the server does not answer: the agenda keeps its agents
          }
          await this.#sendAgents(agents, id, generation)
        })
        if (outcome === "failed") failed = true
        if (outcome === "stale") return
        if (outcome === "signedOut") return this.#signedOut()
      }
      if (generation === this.#generation) this.#setNotice(failed ? WRITE_NOTICE : null)
    })
  }

  /** A change reported by `ServersModel.onChange`. */
  serverChanged(event: ServerChange): void {
    if (event.kind === "removed") this.#serverRemoved(event.server)
    else this.#serverUpserted(event.server.id)
  }

  /** The agents of a server, as the screen read them: only the latest list is sent (`PUT`, the first 50). */
  agentsChanged(serverId: string, agents: AgentDetail[]): void {
    if (!this.#active()) return
    const queued = this.#pendingAgents.has(serverId)
    this.#pendingAgents.set(serverId, agents)
    if (queued) return
    this.#enqueue(async (generation) => {
      let took = false
      // Already sent by an earlier job (it takes the latest list): nothing to write.
      if (!this.#pendingAgents.has(serverId)) return
      await this.#run(generation, WRITE_NOTICE, async () => {
        const id = await this.#upsert(serverId, generation)
        if (id === null) return
        const latest = this.#pendingAgents.get(serverId)
        if (latest === undefined) return
        this.#pendingAgents.delete(serverId)
        took = true
        await this.#sendAgents(latest, id, generation)
      })
      // Failed before sending: drop it (the next load sends the list again), so later changes queue anew.
      if (!took && generation === this.#generation) this.#pendingAgents.delete(serverId)
    })
  }

  /** Drops every queued job and stops the running one from writing (sign out, account deleted). */
  invalidate(): void {
    this.#generation += 1
    this.#chain = Promise.resolve()
    this.#pendingAgents.clear()
    this.#queuedServers.clear()
    this.#pushAllQueued = false
    this.#setNotice(null)
  }

  /** `invalidate()`, and nothing new is queued until `resume()`: for the time a deletion or a sign out takes. */
  suspend(): void {
    this.invalidate()
    this.#suspended = true
  }

  /** Writes need a signed-in session too, so a `resume()` after a sign out leaves the mirror idle. */
  resume(): void {
    this.#suspended = false
  }

  /** Waits until the queue is empty (tests, and callers that need the agenda written). */
  async idle(): Promise<void> {
    let seen: Promise<void>
    do {
      seen = this.#chain
      await seen
    } while (seen !== this.#chain)
  }

  // Reading

  /**
   * The agenda's servers with no token in this browser (compared by canonical URL), to set up here. Entries whose address
   * the app would not use (`http://` off localhost: the Cloud accepts any) are left out.
   */
  async fromAccount(): Promise<CloudServer[]> {
    if (this.#session.state !== "signedIn") return []
    const generation = this.#generation
    try {
      const entries = await this.#session.cloudClient.servers(await this.#token())
      const local = (await this.#store.list()).map((server) => server.url)
      if (generation !== this.#generation) return []
      this.#setNotice(null)
      return entries.filter((entry) => parseServerAddress(entry.url) !== null && !local.some((url) => sameUrl(url, entry.url)))
    } catch (error) {
      if (generation !== this.#generation) return []
      this.#refusedToken(error)
      if (isSignedOut(error)) this.#signedOut()
      else this.#setNotice(READ_NOTICE)
      return []
    }
  }

  // Plumbing

  #active(): boolean {
    return !this.#suspended && this.#session.state === "signedIn"
  }

  #setNotice(notice: string | null): void {
    if (notice === this.#notice) return
    this.#notice = notice
    for (const listener of [...this.#noticeListeners]) {
      try {
        listener()
      } catch {
        // A screen's failure does not reach the mirror.
      }
    }
  }

  #signedOut(): void {
    // The session itself tells the account model (`AccountSession` ends it when the refresh token is refused).
    this.#setNotice(null)
  }

  #enqueue(work: (generation: number) => Promise<void>): void {
    const generation = this.#generation
    const previous = this.#chain
    // A stale job still runs: it only clears its own bookkeeping, `#check` stops every write.
    this.#chain = previous.then(() => work(generation)).catch(() => {})
  }

  #check(generation: number): void {
    if (generation !== this.#generation) throw new Stale()
  }

  async #attempt(generation: number, work: () => Promise<void>): Promise<Outcome> {
    if (generation !== this.#generation) return "stale"
    try {
      await work()
      return generation === this.#generation ? "done" : "stale"
    } catch (error) {
      // A stale job's failure is nobody's business.
      if (generation !== this.#generation || error instanceof Stale) return "stale"
      this.#refusedToken(error)
      if (isSignedOut(error)) return "signedOut"
      return "failed" // the error is not kept: it says nothing the user can act on
    }
  }

  async #run(generation: number, failure: string, work: () => Promise<void>): Promise<void> {
    switch (await this.#attempt(generation, work)) {
      case "done":
        this.#setNotice(null)
        break
      case "failed":
        this.#setNotice(failure)
        break
      case "stale":
        break
      case "signedOut":
        this.#signedOut()
        break
    }
  }

  async #token(): Promise<string> {
    const token = await this.#session.accessToken()
    this.#lastToken = token
    return token
  }

  /** The Cloud refused the access token (deleted in another tab, say): the session renews, or signs out. */
  #refusedToken(error: unknown): void {
    if (isUnauthorized(error) && this.#lastToken !== null) void this.#session.accessTokenRefused(this.#lastToken)
  }

  #serverUpserted(id: string): void {
    if (!this.#active() || this.#queuedServers.has(id)) return
    this.#queuedServers.add(id)
    this.#enqueue(async (generation) => {
      this.#queuedServers.delete(id)
      await this.#run(generation, WRITE_NOTICE, async () => {
        await this.#upsert(id, generation)
      })
    })
  }

  /** Deletes the entry (already gone counts as done). Without a known id, the entry is found by its URL. */
  #serverRemoved(removed: ServerChange["server"]): void {
    this.#pendingAgents.delete(removed.id)
    if (!this.#active()) return
    this.#enqueue(async (generation) => {
      await this.#run(generation, WRITE_NOTICE, async () => {
        const token = await this.#token()
        const cloud = this.#session.cloudClient
        let id = removed.cloudServerId
        if (id === null) {
          id = (await cloud.servers(token)).find((entry) => sameUrl(entry.url, removed.url))?.id ?? null
        }
        if (id === null) return
        this.#check(generation)
        await cloud.removeServer(token, id)
      })
    })
  }

  /**
   * The agenda id of the server `id` as it is now (`null` when it was removed meanwhile): `PATCH` of the known entry, or
   * `POST` (a `409` gives the existing one, which is then patched to this server's name and link). The id is kept in the store.
   */
  async #upsert(id: string, generation: number): Promise<string | null> {
    const server = await this.#store.get(id)
    if (server === null) return null
    const token = await this.#token()
    const cloud = this.#session.cloudClient
    const name = AgendaSync.cut(server.name, hostOf(server.url) || "Server")
    if (server.cloudServerId !== null) {
      try {
        this.#check(generation)
        await cloud.updateServer(token, server.cloudServerId, { name, linked: server.linked })
        return server.cloudServerId
      } catch (error) {
        // Deleted from the agenda elsewhere: added again below.
        if (!(error instanceof ApiError && error.status === 404)) throw error
      }
    }
    this.#check(generation)
    const entry = await cloud.addServer(token, { name, url: server.url, linked: server.linked })
    if (entry.linked !== server.linked || entry.name !== name) {
      this.#check(generation)
      await cloud.updateServer(token, entry.id, { name, linked: server.linked })
    }
    this.#check(generation)
    await this.#store.update(server.id, { cloudServerId: entry.id })
    return entry.id
  }

  async #sendAgents(agents: AgentDetail[], cloudId: string, generation: number): Promise<void> {
    const token = await this.#token()
    this.#check(generation)
    await this.#session.cloudClient.putAgents(token, cloudId, AgendaSync.snapshot(agents))
  }

  /** The first 50 agents the Cloud would accept, with names cut to its limit. */
  static snapshot(agents: AgentDetail[]): CloudAgent[] {
    return agents
      .filter(
        (agent) =>
          typeof agent.id === "string" &&
          typeof agent.slug === "string" &&
          typeof agent.icon === "string" &&
          ID_SHAPE.test(agent.id) &&
          ID_SHAPE.test(agent.slug) &&
          ICON_SHAPE.test(agent.icon) &&
          CALL_TYPES.has(agent.call_type),
      )
      .slice(0, AgendaSync.agentLimit)
      .map((agent) => ({
        id: agent.id,
        slug: agent.slug,
        display_name: AgendaSync.cut(String(agent.display_name ?? ""), agent.slug),
        icon: agent.icon,
        call_type: agent.call_type,
        // The Cloud refuses the whole agenda for a color it does not know (a newer server's), so only known names go.
        color: AGENT_COLORS.find((name) => name === agent.color) ?? null,
      }))
  }

  /** At most 64 Unicode scalars (what the Cloud counts) without splitting a character; `fallback` (cut too) when nothing is left. */
  static cut(text: string, fallback: string): string {
    const result = cutText(text, AgendaSync.textLimit)
    return result === "" ? cutText(fallback, AgendaSync.textLimit) : result
  }
}
