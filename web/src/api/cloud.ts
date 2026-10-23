import { canonical, parseServerAddress } from "../lib/address"
import { ApiError } from "../lib/errors"
import { json, request, type RequestOptions } from "../lib/http"

export interface CloudConfig {
  issuer: string
  project_id: string | null
  clients: { ios?: string; pwa?: string; watch?: string }
  scopes: string[]
  server_tokens: boolean
  push: { apns: boolean; webpush: boolean; vapid_public_key: string | null }
}

export interface CloudAgent {
  id: string
  slug: string
  display_name: string
  icon: string
  call_type: string
  color?: string | null
}

/** What the Cloud hands a signed-in device: its tokens, the access token valid until `expires_at` (epoch seconds). */
export interface DeviceSession {
  account: string
  device: string
  access_token: string
  refresh_token: string
  expires_at: number
}

export interface WebSessionStart {
  id: string
  approve_key: string
  poll_key: string
  expires_at: number
}

export type WebSessionPoll = { status: "pending"; expires_at: number } | ({ status: "approved" } & DeviceSession)

export interface CloudDevice {
  id: string
  name: string
  platform: string
  created_at: number
  last_seen_at: number | null
  current: boolean
}

export interface CloudPlan {
  plan: string | null
  source: string | null
  expires_at: number | null
}

/** The month's call minutes of a Cloud plan, read from a node (`GET /v1/me`). */
export interface CloudUsage {
  month: string
  used_s: number
  quota_s: number
  remaining_s: number
}

/** A node and a per-server token for it (`expires_at` in epoch seconds). */
export interface RouteTarget {
  node: { id: string; url: string; region: string }
  token: string
  expires_at: number
}

/** The answer of `POST /v1/route`: the node to use and, when a second one can take the request, a fallback. */
export interface CloudRoute extends RouteTarget {
  fallback: RouteTarget | null
}

export interface CloudAccount {
  account: string
  created_at: string
  servers: number
  plan: CloudPlan | null
  identities: string[]
  key_set: boolean
}

/** A rotation the Cloud may finish after the default limit: aborting early would lose the new refresh token. */
const REFRESH_TIMEOUT_MS = 60_000

const WEB = "web"

/** `X-Wristcall-Client` of this app: the Cloud shows the Cloud entry only to a recent enough client. */
export const CLIENT = `${WEB}/${__WC_VERSION__}`

export interface CloudServer {
  id: string
  name: string
  url: string
  kind: "self-hosted" | "cloud"
  linked: boolean
  agents: CloudAgent[]
  created_at: string
  updated_at: string
}

/** One path segment: `/`, `?` and `#` are escaped, and `.`/`..` (which a URL would resolve) are refused. */
function segment(id: string): string {
  if (id === "" || id === "." || id === "..") throw new Error("Invalid identifier.")
  return encodeURIComponent(id)
}

const BAD_ROUTE = "The Cloud's route was not understood."

function isTarget(value: unknown): value is RouteTarget {
  if (typeof value !== "object" || value === null) return false
  const target = value as Record<string, unknown>
  const node = target.node as Record<string, unknown> | null | undefined
  return (
    typeof node === "object" &&
    node !== null &&
    typeof node.id === "string" &&
    typeof node.url === "string" &&
    typeof target.token === "string" &&
    typeof target.expires_at === "number"
  )
}

/** The server address in its canonical form when it parses; otherwise as given (the Cloud answers `422`). */
function canonicalOrSame(url: string): string {
  const parsed = parseServerAddress(url)
  return parsed === null ? url : canonical(parsed)
}

/**
 * The Wristcall Cloud API (`cloud/README.md`, "API"). `/v1/config` takes no token; every other route takes the
 * account's access token, passed per call and kept in no field. Every request names this app (`X-Wristcall-Client`).
 */
export class CloudClient {
  readonly #base: string
  readonly #fetch: typeof fetch | undefined
  readonly #client: string

  constructor(base: string, fetchImpl?: typeof fetch, client: string = CLIENT) {
    this.#base = base.replace(/\/+$/, "")
    this.#fetch = fetchImpl
    this.#client = client
  }

  /** The Cloud's address, without a trailing slash. */
  get url(): string {
    return this.#base
  }

  config(): Promise<CloudConfig> {
    return this.#json<CloudConfig>(this.#url("v1/config"), {})
  }

  account(accessToken: string): Promise<CloudAccount> {
    return this.#json(this.#url("v1/account"), { bearer: accessToken })
  }

  /** Starts a web sign-in: the QR carries `id` and `approve_key`; `poll_key` stays in the browser. No token. */
  createWebSession(name: string): Promise<WebSessionStart> {
    return this.#json(this.#url("v1/web-sessions"), { method: "POST", json: { device: { name, platform: WEB } } })
  }

  /** `pending`, or `approved` with the device session (once). `404`: expired or already taken (`ApiError`). */
  pollWebSession(id: string, pollKey: string): Promise<WebSessionPoll> {
    return this.#json(this.#url(`v1/web-sessions/${segment(id)}`), { bearer: pollKey })
  }

  /** Trades a fresh Zitadel access token for a `web` device of the token's account. */
  exchangeSignIn(zitadelAccessToken: string, name: string): Promise<DeviceSession & { key_set: boolean }> {
    return this.#json(this.#url("v1/devices"), { method: "POST", bearer: zitadelAccessToken, json: { device: { name, platform: WEB } } })
  }

  /** Rotates the refresh token: the old one stops working, and reusing it signs the device out. */
  refreshSession(refreshToken: string): Promise<DeviceSession> {
    return this.#json(this.#url("v1/session/refresh"), { method: "POST", json: { refresh_token: refreshToken }, timeoutMs: REFRESH_TIMEOUT_MS })
  }

  async devices(accessToken: string): Promise<CloudDevice[]> {
    return (await this.#json<{ devices: CloudDevice[] }>(this.#url("v1/devices"), { bearer: accessToken })).devices
  }

  /** `404` counts as done. */
  async removeDevice(accessToken: string, id: string): Promise<void> {
    try {
      await this.#request(this.#url(`v1/devices/${segment(id)}`), { method: "DELETE", bearer: accessToken })
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return
      throw error
    }
  }

  redeemInvite(accessToken: string, code: string): Promise<CloudPlan> {
    return this.#json(this.#url("v1/entitlements/redeem"), { method: "POST", bearer: accessToken, json: { code } })
  }

  /** Deletes the agenda and the sign-in. `503 account_unavailable` is an `ApiError`: the same call again finishes it. */
  async deleteAccount(accessToken: string): Promise<void> {
    await this.#request(this.#url("v1/account"), { method: "DELETE", bearer: accessToken })
  }

  async servers(accessToken: string): Promise<CloudServer[]> {
    return (await this.#json<{ servers: CloudServer[] }>(this.#url("v1/servers"), { bearer: accessToken })).servers
  }

  /** `POST /v1/servers`, with the URL in its canonical form. `409` (URL already in the agenda) gives the existing entry. */
  async addServer(accessToken: string, body: { name: string; url: string; kind?: string; linked?: boolean }): Promise<CloudServer> {
    const sent = { ...body, url: canonicalOrSame(body.url) }
    try {
      return await this.#json<CloudServer>(this.#url("v1/servers"), { method: "POST", bearer: accessToken, json: sent })
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error
      const existing = (await this.servers(accessToken)).find((server) => canonicalOrSame(server.url) === sent.url)
      if (existing === undefined) throw error
      return existing
    }
  }

  async updateServer(accessToken: string, id: string, patch: { name?: string; linked?: boolean }): Promise<CloudServer> {
    return await this.#json(this.#url(`v1/servers/${segment(id)}`), { method: "PATCH", bearer: accessToken, json: patch })
  }

  /** `404` counts as done. */
  async removeServer(accessToken: string, id: string): Promise<void> {
    try {
      await this.#request(this.#url(`v1/servers/${segment(id)}`), { method: "DELETE", bearer: accessToken })
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return
      throw error
    }
  }

  /** The full list of the server's agents. */
  async putAgents(accessToken: string, id: string, agents: CloudAgent[]): Promise<void> {
    await this.#request(this.#url(`v1/servers/${segment(id)}/agents`), { method: "PUT", bearer: accessToken, json: { agents } })
  }

  /** A token for one server. `audience` is the server's URL; it is sent in its canonical form. */
  serverToken(accessToken: string, audience: string): Promise<{ token: string; audience: string; expires_at: number }> {
    return this.#json(this.#url("v1/server-tokens"), { method: "POST", bearer: accessToken, json: { audience: canonicalOrSame(audience) } })
  }

  /** A node for the account (`POST /v1/route`): `agent` only for `call`, `avoid` the nodes that just failed. */
  async route(accessToken: string, body: { purpose: "call" | "manage"; agent?: string; avoid?: string[] }): Promise<CloudRoute> {
    const sent: Record<string, unknown> = { purpose: body.purpose }
    if (body.agent !== undefined) sent.agent = body.agent
    if (body.avoid !== undefined) sent.avoid = body.avoid
    const reply = await this.#json<CloudRoute>(this.#url("v1/route"), { method: "POST", bearer: accessToken, json: sent })
    if (!isTarget(reply) || (reply.fallback != null && !isTarget(reply.fallback))) throw new ApiError(200, "bad_response", BAD_ROUTE)
    return { ...reply, fallback: reply.fallback ?? null }
  }

  #url(path: string): string {
    return `${this.#base}/${path}`
  }

  #json<T>(url: string, opts: RequestOptions): Promise<T> {
    return json<T>(url, { ...opts, headers: { "X-Wristcall-Client": this.#client } }, this.#fetch)
  }

  #request(url: string, opts: RequestOptions): Promise<Response> {
    return request(url, { ...opts, headers: { "X-Wristcall-Client": this.#client } }, this.#fetch)
  }
}
