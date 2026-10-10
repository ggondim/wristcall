import { ApiError, userMessage } from "../lib/errors"
import { REQUEST_ID } from "../lib/ids"
import { unixNow } from "../lib/time"
import type { ApprovalRequest } from "../api/server"
import { BaseModel, type Model } from "../ui/useModel"
import type { ServerRow, ServersModel } from "./servers"

export interface PendingApproval {
  serverId: string
  serverName: string
  request: ApprovalRequest
}

export interface ApprovalsState {
  /** Waiting requests, in server order, then by expiry. */
  pending: PendingApproval[]
  /** What the last approve, deny or notification could not do, for the screen. Null clears it. */
  notice: string | null
  /** The request a notification pointed at (`serverId/requestId`, see `approvalKey`); null when none or it is gone. */
  focused: string | null
}

export const EXPIRED_NOTICE = "This request expired."

/** How far apart two expiries can be and still be the same request (the push and the server round differently). */
const SAME_EXPIRY_SECONDS = 1

export function approvalKey(serverId: string, requestId: string): string {
  return `${serverId}/${requestId}`
}

/**
 * Login requests from watches that wait for the owner's approval, across all servers. Approving hands a device
 * credential to whoever started the request, so every path checks the request id (`^[0-9]{4}$`) before it sends it.
 * `now` returns Unix SECONDS (the unit of `expires_at`); the default is `Date.now() / 1000`.
 */
export class ApprovalsModel extends BaseModel<ApprovalsState> {
  readonly #servers: ServersModel
  readonly #now: () => number
  readonly #busy = new Set<string>()
  #loading: Promise<void> | null = null
  #refreshesRunning = 0
  /**
   * Requests answered while a refresh was in flight: the refresh read the list before the answer and must not bring
   * them back. Cleared when no refresh is running (the 4 digit ids repeat over time).
   */
  #answeredDuringRefresh = new Set<string>()

  constructor(servers: ServersModel, now: () => number = unixNow) {
    super({ pending: [], notice: null, focused: null })
    this.#servers = servers
    this.#now = now
  }

  /**
   * Asks every server that said it has an account, in parallel, for its waiting requests. A server that answers
   * `not_configured` (or does not accept the token) has none; a failure to connect keeps what was shown.
   */
  async refresh(): Promise<void> {
    this.#refreshesRunning++
    try {
      await this.#ensureServers()
      // An old server answers 404 to the route, and a server whose health is unknown has nothing to ask yet.
      const targets = this.#servers.state.rows.filter((row) => row.health?.account != null)
      type Answer = { ok: true; row: ServerRow; requests: ApprovalRequest[] } | { ok: false; row: ServerRow; error: unknown }
      const results = await Promise.all(
        targets.map(async (row): Promise<Answer> => {
          try {
            const client = await this.#servers.client(row.server.id)
            if (client === null) return { ok: true, row, requests: [] }
            return { ok: true, row, requests: await client.pairingRequests() }
          } catch (error) {
            return { ok: false, row, error }
          }
        }),
      )
      const current = this.#now()
      const merged: PendingApproval[] = []
      for (const row of this.#servers.state.rows) {
        const id = row.server.id
        const name = row.server.name
        const answered = (request: ApprovalRequest) => this.#answeredDuringRefresh.has(approvalKey(id, request.request_id))
        const keep = () =>
          this.state.pending.filter((item) => item.serverId === id && item.request.expires_at > current && !answered(item.request))
        // A server whose health is not known yet (still being checked, or unreachable) keeps what it had.
        if (row.health === null) {
          merged.push(...keep())
          continue
        }
        const result = results.find((candidate) => candidate.row.server.id === id)
        if (result === undefined) continue // no account: nothing to ask
        if (result.ok) {
          merged.push(
            ...result.requests
              .filter((request) => REQUEST_ID.test(request.request_id) && request.expires_at > current && !answered(request))
              .map((request) => ({ serverId: id, serverName: name, request }))
              .sort((a, b) => a.request.expires_at - b.request.expires_at),
          )
        } else if (!hasNone(result.error)) {
          // A blip: keep what that server had.
          merged.push(...keep())
        }
      }
      if (!samePending(merged, this.state.pending)) this.set({ pending: merged })
    } finally {
      if (--this.#refreshesRunning === 0) this.#answeredDuringRefresh = new Set()
    }
  }

  approve(item: PendingApproval): Promise<void> {
    return this.#answer("approve", item.serverId, item.request.request_id)
  }

  deny(item: PendingApproval): Promise<void> {
    return this.#answer("deny", item.serverId, item.request.request_id)
  }

  /**
   * A notification opened the approvals screen with `?server=&request=&expires=`. The server is found by the local id
   * the push carried (its `tag`), and only the id and the expiry are used; an unknown server or a malformed id does
   * nothing. The request is highlighted only when the server lists it now with the same expiry (the 4 digit ids are
   * reused, so the same id with another expiry is a different request); otherwise it expired. `expiresAt` is in
   * Unix seconds; null when the push did not say.
   */
  async focus(serverId: string, requestId: string, expiresAt: number | null): Promise<void> {
    if (!REQUEST_ID.test(requestId)) return
    if ((await this.#servers.client(serverId)) === null) return
    if (expiresAt !== null && !(expiresAt > this.#now())) {
      this.set({ notice: EXPIRED_NOTICE, focused: null })
      return
    }
    await this.refresh()
    const found = this.state.pending.find(
      (item) =>
        item.serverId === serverId &&
        item.request.request_id === requestId &&
        (expiresAt === null || Math.abs(item.request.expires_at - expiresAt) <= SAME_EXPIRY_SECONDS),
    )
    if (found === undefined) this.set({ notice: EXPIRED_NOTICE, focused: null })
    else this.set({ notice: null, focused: approvalKey(serverId, requestId) })
  }

  clearNotice(): void {
    if (this.state.notice !== null) this.set({ notice: null })
  }

  async #answer(action: "approve" | "deny", serverId: string, requestId: string): Promise<void> {
    if (!REQUEST_ID.test(requestId)) return
    const key = approvalKey(serverId, requestId)
    if (this.#busy.has(key)) return
    this.#busy.add(key)
    try {
      const client = await this.#servers.client(serverId)
      if (client === null) {
        this.#drop(serverId, requestId)
        return
      }
      try {
        if (action === "approve") await client.approve(requestId)
        else await client.deny(requestId)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          this.#drop(serverId, requestId)
          this.set({ notice: EXPIRED_NOTICE })
        } else {
          this.set({ notice: userMessage(e) })
        }
        return
      }
      this.#drop(serverId, requestId)
      this.set({ notice: null })
    } finally {
      this.#busy.delete(key)
    }
  }

  #drop(serverId: string, requestId: string): void {
    const key = approvalKey(serverId, requestId)
    if (this.#refreshesRunning > 0) this.#answeredDuringRefresh.add(key)
    this.set({
      pending: this.state.pending.filter((item) => !(item.serverId === serverId && item.request.request_id === requestId)),
      focused: this.state.focused === key ? null : this.state.focused,
    })
  }

  /** The servers are read once if nothing has read them yet (a notification can open the app straight on this screen). */
  async #ensureServers(): Promise<void> {
    // The list is marked loaded before the health checks end: while our own read runs, wait for it to finish.
    if (this.#loading === null) {
      if (this.#servers.state.loaded) return
      this.#loading = this.#servers.load().finally(() => {
        this.#loading = null
      })
    }
    await this.#loading
  }
}

/**
 * Reads the approvals again whenever the set of servers that can have them changes: one added, removed, or found (by
 * its health check) to accept an account. Without it the badge would miss a server saved or checked after the
 * first read. Returns the unsubscribe.
 */
export function refreshWhenServersChange(servers: ServersModel, approvals: ApprovalsModel): () => void {
  const signature = () =>
    servers.state.rows
      .filter((row) => row.health?.account != null)
      .map((row) => row.server.id)
      .join("\n")
  let known = signature()
  return servers.subscribe(() => {
    const now = signature()
    if (now === known) return
    known = now
    void approvals.refresh()
  })
}

/** The server has no requests for this token: no account, or the token is not accepted. */
function hasNone(error: unknown): boolean {
  return error instanceof ApiError && [401, 403, 404].includes(error.status)
}

function samePending(a: PendingApproval[], b: PendingApproval[]): boolean {
  return (
    a.length === b.length &&
    a.every(
      (item, i) =>
        item.serverId === b[i]!.serverId &&
        item.serverName === b[i]!.serverName &&
        item.request.request_id === b[i]!.request.request_id &&
        item.request.expires_at === b[i]!.request.expires_at &&
        item.request.device_name === b[i]!.request.device_name,
    )
  )
}

/** The count the Servers tab shows: how many requests wait. */
export class ApprovalsBadge implements Model<{ count: number }> {
  readonly #approvals: ApprovalsModel
  #state: { count: number }

  constructor(approvals: ApprovalsModel) {
    this.#approvals = approvals
    this.#state = { count: approvals.state.pending.length }
  }

  get state(): { count: number } {
    const count = this.#approvals.state.pending.length
    if (count !== this.#state.count) this.#state = { count }
    return this.#state
  }

  subscribe(listener: () => void): () => void {
    return this.#approvals.subscribe(listener)
  }
}
