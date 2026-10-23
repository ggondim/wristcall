import type { CloudClient, CloudRoute } from "../api/cloud"
import { ApiError } from "../lib/errors"

/** What the router needs from the sign-in: a fresh access token and the Cloud client (the real `AccountSession`). */
export interface RouterSession {
  accessToken(): Promise<string>
  /** Told about an access token the Cloud answered `401` to, so the session renews it (or signs out). */
  accessTokenRefused?(token: string): Promise<void>
  readonly cloudClient: CloudClient
}

/** A `manage` route is used until this long before it expires (the token lives 300 s). */
const MARGIN_MS = 30_000

/**
 * The node that serves the account's Cloud entry (`POST /v1/route`, `cloud/README.md`). A `manage` route is kept until
 * 30 s before it expires, and requests at the same time share one trip. `invalidate()` (a `401` from the node, signing
 * out) drops it, and a route asked for before that is handed to its caller but not kept: no route of one account
 * outlives it. A failure (`503 no_node_available`, say) is not kept either.
 */
export class CloudRouter {
  readonly #session: RouterSession
  readonly #now: () => number
  #route: CloudRoute | null = null
  #pending: Promise<CloudRoute> | null = null
  /** Bumped by `invalidate()`: a trip that started before it keeps nothing. */
  #generation = 0

  constructor(session: RouterSession, now: () => number = () => Date.now()) {
    this.#session = session
    this.#now = now
  }

  manage(): Promise<CloudRoute> {
    const route = this.#route
    if (route !== null && this.#now() < route.expires_at * 1000 - MARGIN_MS) return Promise.resolve(route)
    this.#route = null
    if (this.#pending !== null) return this.#pending
    const pending: Promise<CloudRoute> = this.#ask(this.#generation).finally(() => {
      if (this.#pending === pending) this.#pending = null
    })
    this.#pending = pending
    return pending
  }

  invalidate(): void {
    this.#generation += 1
    this.#route = null
    this.#pending = null
  }

  async #ask(generation: number): Promise<CloudRoute> {
    let token: string | null = null
    try {
      token = await this.#session.accessToken()
      const fresh = await this.#session.cloudClient.route(token, { purpose: "manage" })
      if (generation === this.#generation) this.#route = fresh
      return fresh
    } catch (error) {
      // The Cloud refused the session's access token: the session renews it (or signs out).
      if (token !== null && error instanceof ApiError && error.status === 401) void this.#session.accessTokenRefused?.(token).catch(() => {})
      throw error
    }
  }
}
