import { ApiError, userMessage } from "../lib/errors"
import { saveFile, type SaveResult } from "../lib/saveFile"
import type { AgentDetail, CallRecord, CallsPage, CallsQuery } from "../api/server"
import type { SavedServer } from "../store/servers"
import { BaseModel } from "../ui/useModel"
import type { ServersModel } from "./servers"

export interface HistoryFilter {
  /** Search text, matched by the servers against what was said. */
  q: string
  serverId: string | null
  /** An agent id or slug of the chosen server; null while no server is chosen. */
  agent: string | null
  /** Unix seconds. */
  since: number | null
  /** Unix seconds, excluded. */
  until: number | null
}

export interface HistoryItem {
  serverId: string
  serverName: string
  call: CallRecord
}

/** A server whose page is missing; the other servers' calls are still listed. */
export interface HistoryFailure {
  serverId: string
  serverName: string
  message: string
}

export interface ExportedFile {
  filename: string
  blob: Blob
}

/** The call a detail screen shows: found in the list, or read from its server when the address was opened directly. */
export interface HistoryDetail {
  status: "loading" | "ready" | "missing"
  item: HistoryItem | null
}

/** Ready made periods for the filter menu (a window that ends now). */
export type Period = "anytime" | "day" | "week" | "month"
export const PERIODS: { id: Period; title: string; seconds: number | null }[] = [
  { id: "anytime", title: "Any time", seconds: null },
  { id: "day", title: "Last 24 hours", seconds: 86_400 },
  { id: "week", title: "Last 7 days", seconds: 7 * 86_400 },
  { id: "month", title: "Last 30 days", seconds: 30 * 86_400 },
]

export interface HistoryState {
  /** Newest first; a tie on the date is broken by the call id (greatest first), like the servers' own cursor. */
  items: HistoryItem[]
  loading: boolean
  loadingMore: boolean
  /** True while some server still has an older page. */
  more: boolean
  /** True once the first read finished. */
  loaded: boolean
  /** What the last action could not do. */
  error: string | null
  /** What the last "delete all" did. */
  notice: string | null
  failures: HistoryFailure[]
  filter: HistoryFilter
  /** The period menu's choice; `filter.since` is derived from it. */
  period: Period
  /** The agents of the chosen server (the filter menu); empty while no server is chosen. */
  agents: AgentDetail[]
  detail: HistoryDetail | null
  /** An export the browser would not share without a new tap; the screen offers a button for it. */
  exported: ExportedFile | null
}

export const EMPTY_FILTER: HistoryFilter = { q: "", serverId: null, agent: null, since: null, until: null }
export const PAGE_SIZE = 50
export const SERVER_REMOVED = "That server was removed."
export const CHOOSE_AGENT = "Choose an agent."
/** How long a read stays fresh: coming back to the tab inside this window does not read again. */
const FRESH_MS = 15_000
const MAX_RELOAD_WAIT_MS = 30_000

export function itemKey(serverId: string, callId: string): string {
  return `${serverId}/${callId}`
}

/** Whether a filter narrows the list at all. */
export function filterIsActive(filter: HistoryFilter): boolean {
  return filter.serverId !== null || filter.q.trim() !== "" || filter.since !== null || filter.until !== null
}

const REDELIVERABLE = new Set(["delivery_failed", "interrupted"])

/** What `/redeliver` accepts (the iPhone's `canRedeliver`): a one-way call whose delivery failed or was cut short, with text. The server decides in the end. */
export function canRedeliver(call: CallRecord): boolean {
  return (
    (call.call_type === "one-shot" || call.call_type === "monologue") &&
    call.status === "failed" &&
    typeof call.error === "string" &&
    REDELIVERABLE.has(call.error) &&
    typeof call.text === "string" &&
    call.text.trim() !== ""
  )
}

/** Why a call or entry failed, in words (the iPhone's `CallStatusStyle.reason`). */
export function reasonText(code: string): string {
  switch (code) {
    case "delivery_failed":
      return "not delivered"
    case "interrupted":
      return "interrupted"
    case "stt_failed":
      return "not transcribed"
    case "responder_failed":
      return "no answer"
    case "tts_failed":
      return "no voice"
    case "unreadable":
      return "unreadable"
    default:
      return code.replace(/_/g, " ")
  }
}

export function statusLabel(call: CallRecord): string {
  switch (call.status) {
    case "delivered":
      return "Delivered"
    case "failed":
      return typeof call.error === "string" && call.error !== "" ? `Failed, ${reasonText(call.error)}` : "Failed"
    case "processing":
      return "Processing"
    case "recording":
      return "Recording"
    case "empty":
      return "Nothing said"
    case "ended":
      return "Ended"
    default:
      return call.status === "" ? call.status : call.status.charAt(0).toUpperCase() + call.status.slice(1)
  }
}

/** delivered green, failed red, everything else gray: the word is always shown, so the colour is never the only cue. */
export function statusTone(status: string): "ok" | "bad" | "neutral" {
  return status === "delivered" ? "ok" : status === "failed" ? "bad" : "neutral"
}

export function typeLabel(type: string | undefined): string {
  switch (type) {
    case "conversation":
      return "Conversation"
    case "one-shot":
      return "One-shot"
    case "monologue":
      return "Monologue"
    default:
      return type ?? ""
  }
}

/** The reason an entry has no text, with a capital: "Not transcribed". */
export function entryReason(code: string): string {
  const text = reasonText(code)
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** What was said, one line per utterance; a one-way call without entries gives its text. */
export function transcript(call: CallRecord): string {
  const lines: string[] = []
  for (const entry of call.entries ?? []) {
    const who = entry.role === "user" ? "You" : "Agent"
    if (typeof entry.text === "string" && entry.text !== "") lines.push(`${who}: ${entry.text}`)
    else if (typeof entry.error === "string" && entry.error !== "") lines.push(`${who}: (${entry.error})`)
  }
  return lines.length > 0 ? lines.join("\n") : (call.text ?? "")
}

export function agentName(call: CallRecord): string {
  return call.agent?.display_name || "Unknown agent"
}

/** What the server says about a refused redelivery (`docs/protocol.md`, History), by the `error` of the 409. */
const REDELIVER_CONFLICT: Record<string, string> = {
  not_failed: "Only a call whose delivery failed can be delivered again.",
  no_text: "This call has no transcript to deliver.",
  busy: "This call is being delivered right now.",
  not_one_way: "Only one-shot and monologue calls can be delivered again.",
  agent_gone: "The agent of this call was deleted.",
  agent_unavailable: "The agent's webhook is not available. Check its action.",
}
export const CONFLICT_FALLBACK = "The server reported a conflict."

function compare(left: HistoryItem, right: HistoryItem): number {
  const byDate = (right.call.created_at ?? 0) - (left.call.created_at ?? 0)
  if (byDate !== 0) return byDate
  if (left.call.id !== right.call.id) return left.call.id > right.call.id ? -1 : 1
  return left.serverId < right.serverId ? -1 : left.serverId > right.serverId ? 1 : 0
}

/** Hands a file to the user; `needs-tap` when the browser wants a fresh tap first. */
export type Saver = (blob: Blob, filename: string, afterTap?: boolean) => Promise<SaveResult>

type Fetched = { ok: true; page: CallsPage } | { ok: false; message: string }

/**
 * The calls of every server in one list, newest first. Each server is asked for its own page and the pages are joined by
 * date; a server that does not answer is named in `failures` and does not hide the others. The same rules as the iPhone
 * app's history (`watch/Phone/HistoryModel.swift`).
 */
export class HistoryModel extends BaseModel<HistoryState> {
  readonly #servers: ServersModel
  readonly #pageSize: number
  readonly #save: Saver
  readonly #now: () => number
  #records = new Map<string, CallRecord[]>()
  #cursors = new Map<string, string>()
  #names = new Map<string, string>()
  /** The filter the list on screen was read with: "load more" continues it, whatever `filter` says by now. */
  #loadedFilter: HistoryFilter | null = null
  #loadedAt = 0
  /** Counts reads; an answer that arrives after a newer read started belongs to an old filter. */
  #generation = 0
  #agentReads = 0
  #opens = 0

  constructor(
    servers: ServersModel,
    options: { pageSize?: number; save?: Saver; now?: () => number } = {},
  ) {
    super({
      items: [],
      loading: false,
      loadingMore: false,
      more: false,
      loaded: false,
      error: null,
      notice: null,
      failures: [],
      filter: EMPTY_FILTER,
      period: "anytime",
      agents: [],
      detail: null,
      exported: null,
    })
    this.#servers = servers
    this.#pageSize = options.pageSize ?? PAGE_SIZE
    this.#save = options.save ?? saveFile
    this.#now = options.now ?? Date.now
    // A server removed or renamed elsewhere changes what the list may show.
    servers.onChange((event) => {
      if (event.kind === "removed") {
        this.#records.delete(event.server.id)
        this.#cursors.delete(event.server.id)
        this.#names.delete(event.server.id)
        this.#loadedAt = 0
        this.#rebuild()
      } else if (event.kind === "updated") {
        this.#names.set(event.server.id, event.server.name)
        this.#rebuild()
      } else {
        this.#loadedAt = 0
      }
    })
  }

  // Reading

  /** Reads the first page of every server, unless the list is fresh from a moment ago. */
  async load(): Promise<void> {
    if (this.state.loaded && !this.state.loading && this.#now() - this.#loadedAt < FRESH_MS) return
    await this.reload()
  }

  /** First page of every server the filter covers, in parallel. Restarts the list. */
  async reload(): Promise<void> {
    const mine = ++this.#generation // invalidates every answer still in flight, load more included
    this.set({ loading: true, loadingMore: false })
    const servers = await this.#knownServers()
    if (mine !== this.#generation) return

    // A server that was removed cannot stay chosen.
    let filter = this.state.filter
    if (filter.serverId !== null && !servers.some((server) => server.id === filter.serverId)) {
      filter = { ...filter, serverId: null, agent: null }
      this.set({ filter, agents: [] })
    }
    const targets = servers.filter((server) => filter.serverId === null || server.id === filter.serverId)
    const results = await Promise.all(targets.map((server) => this.#fetch(server, this.#query(filter, undefined))))
    if (mine !== this.#generation) return

    const keepOld = this.#loadedFilter !== null && sameFilter(this.#loadedFilter, filter)
    const records = new Map<string, CallRecord[]>()
    const cursors = new Map<string, string>()
    const failures: HistoryFailure[] = []
    targets.forEach((server, index) => {
      const result = results[index]!
      if (result.ok) {
        records.set(server.id, result.page.calls)
        if (result.page.next_before) cursors.set(server.id, result.page.next_before)
        return
      }
      failures.push({ serverId: server.id, serverName: server.name, message: result.message })
      // A blip on a refresh keeps what that server showed; another filter's calls would mislead.
      if (keepOld) {
        records.set(server.id, this.#records.get(server.id) ?? [])
        const cursor = this.#cursors.get(server.id)
        if (cursor !== undefined) cursors.set(server.id, cursor)
      }
    })
    this.#records = records
    this.#cursors = cursors
    this.#names = new Map(targets.map((server) => [server.id, server.name]))
    this.#loadedFilter = filter
    this.#loadedAt = this.#now()
    this.set({ loading: false, loaded: true, failures })
    this.#rebuild()
  }

  /** The next page of every server that still has one, joined into the list. */
  async loadMore(): Promise<void> {
    // While a read runs, the cursors and the filter on hand are about to be replaced.
    const loaded = this.#loadedFilter
    if (this.state.loading || this.state.loadingMore || loaded === null || this.#cursors.size === 0) return
    const mine = this.#generation
    const jobs = [...this.#cursors.entries()].sort(([a], [b]) => (a < b ? -1 : 1))
    this.set({ loadingMore: true })
    const known = new Map(this.#serverRows().map((server) => [server.id, server]))
    const results = await Promise.all(
      jobs.map(async ([id, before]): Promise<[string, Fetched]> => {
        const server = known.get(id)
        if (server === undefined) return [id, { ok: false, message: SERVER_REMOVED }]
        return [id, await this.#fetch(server, this.#query(loaded, before))]
      }),
    )
    if (mine !== this.#generation) return
    const failed = new Map(this.state.failures.map((failure) => [failure.serverId, failure]))
    for (const [id, result] of results) {
      if (result.ok) {
        const have = this.#records.get(id) ?? []
        const seen = new Set(have.map((call) => call.id))
        this.#records.set(id, [...have, ...result.page.calls.filter((call) => !seen.has(call.id))])
        if (result.page.next_before) this.#cursors.set(id, result.page.next_before)
        else this.#cursors.delete(id)
        failed.delete(id)
      } else {
        // The cursor stays: the next try asks for the same page again.
        failed.set(id, { serverId: id, serverName: this.#names.get(id) ?? id, message: result.message })
      }
    }
    this.set({ loadingMore: false, failures: [...failed.values()] })
    this.#rebuild()
  }

  /**
   * Changes the filter and restarts the list. Choosing another server drops the agent (it only makes sense inside one
   * server). The search text is not debounced here: the screen waits for a pause in typing.
   */
  async setFilter(change: Partial<HistoryFilter>): Promise<void> {
    const before = this.state.filter
    const next: HistoryFilter = { ...before, ...change }
    if (change.serverId !== undefined && change.serverId !== before.serverId && change.agent === undefined) next.agent = null
    if (next.serverId === null) next.agent = null
    this.set({ filter: next })
    if (next.serverId !== before.serverId) void this.#loadAgents(next.serverId)
    await this.reload()
  }

  /** A window that ends now: sets `since` and clears `until`. */
  async setPeriod(period: Period): Promise<void> {
    const seconds = PERIODS.find((entry) => entry.id === period)?.seconds ?? null
    this.set({ period })
    await this.setFilter({ since: seconds === null ? null : Math.floor(this.#now() / 1000) - seconds, until: null })
  }

  async #loadAgents(serverId: string | null): Promise<void> {
    const read = ++this.#agentReads
    this.set({ agents: [] })
    if (serverId === null) return
    try {
      const client = await this.#servers.client(serverId)
      if (client === null) return
      const agents = await client.agents()
      if (read === this.#agentReads) this.set({ agents })
    } catch (e) {
      if (read === this.#agentReads) this.set({ error: userMessage(e) })
    }
  }

  // Changing

  async remove(item: HistoryItem): Promise<void> {
    this.set({ error: null })
    const client = await this.#servers.client(item.serverId)
    if (client === null) return this.set({ error: SERVER_REMOVED })
    try {
      await client.deleteCall(item.call.id)
    } catch (e) {
      // Deleted from elsewhere meanwhile: same result.
      if (!(e instanceof ApiError && e.status === 404)) return this.set({ error: userMessage(e) })
    }
    this.#drop(item.serverId, item.call.id)
  }

  /**
   * Deletes every call of a server, or of one agent of it. The search and the period do not narrow it: the
   * confirmation on screen promises the whole agent. The screen asks before calling this.
   */
  async deleteAll(serverId: string, agent: string | null): Promise<void> {
    this.set({ error: null, notice: null })
    let ref: string | null = null
    if (agent !== null) {
      ref = agent.trim()
      // A blank agent would read as "all".
      if (ref === "") return this.set({ error: CHOOSE_AGENT })
    }
    const client = await this.#servers.client(serverId)
    if (client === null) return this.set({ error: SERVER_REMOVED })
    try {
      const count = await client.deleteCalls(ref === null ? { all: true } : { agent: ref })
      this.set({ notice: count === 1 ? "Deleted 1 call." : `Deleted ${count} calls.` })
    } catch (e) {
      return this.set({ error: userMessage(e) })
    }
    await this.reload()
  }

  /** Sends a failed one-way call again; the list shows the answer (the call, `processing` now). */
  async redeliver(item: HistoryItem): Promise<void> {
    this.set({ error: null })
    if (!canRedeliver(item.call)) return
    const client = await this.#servers.client(item.serverId)
    if (client === null) return this.set({ error: SERVER_REMOVED })
    try {
      this.#apply(item.serverId, await client.redeliver(item.call.id))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) this.set({ error: REDELIVER_CONFLICT[e.code] ?? CONFLICT_FALLBACK })
      else this.set({ error: userMessage(e) })
    }
  }

  /** Reads the call again (follows a redelivery). A blip is not worth an alert: the next try is soon. */
  async refresh(item: HistoryItem): Promise<void> {
    try {
      const client = await this.#servers.client(item.serverId)
      if (client === null) return
      this.#apply(item.serverId, await client.call(item.call.id))
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) this.#drop(item.serverId, item.call.id)
      // Anything else: keep what is on screen.
    }
  }

  /** Points the detail screen at a call: the one in the list, or else read from its server. */
  async open(serverId: string, callId: string): Promise<void> {
    const open = ++this.#opens
    this.set({ error: null })
    const found = this.state.items.find((item) => item.serverId === serverId && item.call.id === callId)
    if (found !== undefined) return this.set({ detail: { status: "ready", item: found } })
    this.set({ detail: { status: "loading", item: null } })
    try {
      const server = (await this.#knownServers()).find((row) => row.id === serverId)
      const client = await this.#servers.client(serverId)
      if (client === null || server === undefined) throw new ApiError(404, "not_found", SERVER_REMOVED)
      const call = await client.call(callId)
      if (open === this.#opens) this.set({ detail: { status: "ready", item: { serverId, serverName: server.name, call } } })
    } catch (e) {
      if (open !== this.#opens) return
      this.set({ detail: { status: "missing", item: null } })
      if (!(e instanceof ApiError && e.status === 404)) this.set({ error: userMessage(e) })
    }
  }

  // Export

  /**
   * The server's own file for a server (and, when it is the chosen one, its agent and period; the search text does not
   * reach the export), handed to the user with the file name the server gave it.
   */
  async exportFile(serverId: string, format: "md" | "json"): Promise<void> {
    this.set({ error: null, exported: null })
    const client = await this.#servers.client(serverId)
    if (client === null) return this.set({ error: SERVER_REMOVED })
    const filter = this.state.filter
    const scoped = filter.serverId === serverId
    const query: Omit<CallsQuery, "q" | "limit" | "before"> = {}
    if (scoped && filter.agent !== null) query.agent = filter.agent
    if (filter.since !== null) query.since = filter.since
    if (filter.until !== null) query.until = filter.until
    let file: ExportedFile
    try {
      file = await client.exportCalls(format, query)
    } catch (e) {
      return this.set({ error: userMessage(e) })
    }
    try {
      if ((await this.#save(file.blob, file.filename)) === "needs-tap") this.set({ exported: file })
    } catch {
      this.set({ error: "The file could not be saved." })
    }
  }

  /** Hands over an export that waited for a tap. */
  async shareExported(): Promise<void> {
    const file = this.state.exported
    if (file === null) return
    this.set({ exported: null, error: null })
    try {
      await this.#save(file.blob, file.filename, true)
    } catch {
      this.set({ error: "The file could not be saved." })
    }
  }

  /** Drops an export nobody took: it holds what was said in the calls. */
  discardExport(): void {
    if (this.state.exported !== null) this.set({ exported: null })
  }

  clearMessages(): void {
    if (this.state.error !== null || this.state.notice !== null) this.set({ error: null, notice: null })
  }

  // Plumbing

  #serverRows(): SavedServer[] {
    return this.#servers.state.rows.map((row) => row.server)
  }

  /** The saved servers; starts reading them when nobody has, without waiting for their health checks. */
  async #knownServers(): Promise<SavedServer[]> {
    if (!this.#servers.state.loaded) {
      await new Promise<void>((resolve) => {
        const off = this.#servers.subscribe(() => {
          if (!this.#servers.state.loaded) return
          off()
          clearTimeout(timer)
          resolve()
        })
        const timer = setTimeout(() => {
          off()
          resolve()
        }, MAX_RELOAD_WAIT_MS)
        if (!this.#servers.state.loading) void this.#servers.load()
      })
    }
    return this.#serverRows()
  }

  #query(filter: HistoryFilter, before: string | undefined): CallsQuery {
    const query: CallsQuery = { limit: this.#pageSize }
    if (filter.q.trim() !== "") query.q = filter.q
    if (filter.serverId !== null && filter.agent !== null) query.agent = filter.agent
    if (filter.since !== null) query.since = filter.since
    if (filter.until !== null) query.until = filter.until
    if (before !== undefined) query.before = before
    return query
  }

  async #fetch(server: SavedServer, query: CallsQuery): Promise<Fetched> {
    try {
      const client = await this.#servers.client(server.id)
      if (client === null) return { ok: false, message: SERVER_REMOVED }
      return { ok: true, page: await client.calls(query) }
    } catch (e) {
      return { ok: false, message: userMessage(e) }
    }
  }

  /** A call came back changed (a redelivery, a refresh): replaces it in the list and on the detail screen. */
  #apply(serverId: string, call: CallRecord): void {
    const list = this.#records.get(serverId)
    const at = list?.findIndex((known) => known.id === call.id) ?? -1
    if (list !== undefined && at >= 0) list[at] = call
    const detail = this.state.detail
    if (detail?.item != null && detail.item.serverId === serverId && detail.item.call.id === call.id) {
      this.set({ detail: { status: "ready", item: { ...detail.item, call } } })
    }
    this.#rebuild()
  }

  #drop(serverId: string, callId: string): void {
    const list = this.#records.get(serverId)
    if (list !== undefined) this.#records.set(serverId, list.filter((call) => call.id !== callId))
    const detail = this.state.detail
    if (detail?.item != null && detail.item.serverId === serverId && detail.item.call.id === callId) {
      this.set({ detail: { status: "missing", item: null } })
    }
    this.#rebuild()
  }

  #rebuild(): void {
    const items: HistoryItem[] = []
    for (const [serverId, calls] of this.#records) {
      const serverName = this.#names.get(serverId) ?? serverId
      for (const call of calls) items.push({ serverId, serverName, call })
    }
    items.sort(compare)
    this.set({ items, more: this.#cursors.size > 0 })
  }
}

function sameFilter(a: HistoryFilter, b: HistoryFilter): boolean {
  return a.q === b.q && a.serverId === b.serverId && a.agent === b.agent && a.since === b.since && a.until === b.until
}
