import { canonical, parseServerAddress } from "../lib/address"
import { ApiError } from "../lib/errors"
import { json, request } from "../lib/http"

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

/** The server address in its canonical form when it parses; otherwise as given (the Cloud answers `422`). */
function canonicalOrSame(url: string): string {
  const parsed = parseServerAddress(url)
  return parsed === null ? url : canonical(parsed)
}

/**
 * The wristcall Cloud API (`cloud/README.md`, "API"). `/v1/config` takes no token; every other route takes the
 * account's access token, passed per call and kept in no field.
 */
export class CloudClient {
  readonly #base: string
  readonly #fetch: typeof fetch | undefined

  constructor(base: string, fetchImpl?: typeof fetch) {
    this.#base = base.replace(/\/+$/, "")
    this.#fetch = fetchImpl
  }

  /** The Cloud's address, without a trailing slash. */
  get url(): string {
    return this.#base
  }

  config(): Promise<CloudConfig> {
    return json<CloudConfig>(this.#url("v1/config"), {}, this.#fetch)
  }

  account(accessToken: string): Promise<CloudAccount> {
    return json(this.#url("v1/account"), { bearer: accessToken }, this.#fetch)
  }

  /** Starts a web sign-in: the QR carries `id` and `approve_key`; `poll_key` stays in the browser. No token. */
  createWebSession(name: string): Promise<WebSessionStart> {
    return json(this.#url("v1/web-sessions"), { method: "POST", json: { device: { name, platform: WEB } } }, this.#fetch)
  }

  /** `pending`, or `approved` with the device session (once). `404`: expired or already taken (`ApiError`). */
  pollWebSession(id: string, pollKey: string): Promise<WebSessionPoll> {
    return json(this.#url(`v1/web-sessions/${segment(id)}`), { bearer: pollKey }, this.#fetch)
  }

  /** Trades a fresh Zitadel access token for a `web` device of the token's account. */
  exchangeSignIn(zitadelAccessToken: string, name: string): Promise<DeviceSession & { key_set: boolean }> {
    return json(this.#url("v1/devices"), { method: "POST", bearer: zitadelAccessToken, json: { device: { name, platform: WEB } } }, this.#fetch)
  }

  /** Rotates the refresh token: the old one stops working, and reusing it signs the device out. */
  refreshSession(refreshToken: string): Promise<DeviceSession> {
    return json(this.#url("v1/session/refresh"), { method: "POST", json: { refresh_token: refreshToken }, timeoutMs: REFRESH_TIMEOUT_MS }, this.#fetch)
  }

  async devices(accessToken: string): Promise<CloudDevice[]> {
    return (await json<{ devices: CloudDevice[] }>(this.#url("v1/devices"), { bearer: accessToken }, this.#fetch)).devices
  }

  /** `404` counts as done. */
  async removeDevice(accessToken: string, id: string): Promise<void> {
    try {
      await request(this.#url(`v1/devices/${segment(id)}`), { method: "DELETE", bearer: accessToken }, this.#fetch)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return
      throw error
    }
  }

  redeemInvite(accessToken: string, code: string): Promise<CloudPlan> {
    return json(this.#url("v1/entitlements/redeem"), { method: "POST", bearer: accessToken, json: { code } }, this.#fetch)
  }

  /** Deletes the agenda and the sign-in. `503 account_unavailable` is an `ApiError`: the same call again finishes it. */
  async deleteAccount(accessToken: string): Promise<void> {
    await request(this.#url("v1/account"), { method: "DELETE", bearer: accessToken }, this.#fetch)
  }

  async servers(accessToken: string): Promise<CloudServer[]> {
    return (await json<{ servers: CloudServer[] }>(this.#url("v1/servers"), { bearer: accessToken }, this.#fetch)).servers
  }

  /** `POST /v1/servers`, with the URL in its canonical form. `409` (URL already in the agenda) gives the existing entry. */
  async addServer(accessToken: string, body: { name: string; url: string; kind?: string; linked?: boolean }): Promise<CloudServer> {
    const sent = { ...body, url: canonicalOrSame(body.url) }
    try {
      return await json<CloudServer>(this.#url("v1/servers"), { method: "POST", bearer: accessToken, json: sent }, this.#fetch)
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error
      const existing = (await this.servers(accessToken)).find((server) => canonicalOrSame(server.url) === sent.url)
      if (existing === undefined) throw error
      return existing
    }
  }

  async updateServer(accessToken: string, id: string, patch: { name?: string; linked?: boolean }): Promise<CloudServer> {
    return await json(this.#url(`v1/servers/${segment(id)}`), { method: "PATCH", bearer: accessToken, json: patch }, this.#fetch)
  }

  /** `404` counts as done. */
  async removeServer(accessToken: string, id: string): Promise<void> {
    try {
      await request(this.#url(`v1/servers/${segment(id)}`), { method: "DELETE", bearer: accessToken }, this.#fetch)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return
      throw error
    }
  }

  /** The full list of the server's agents. */
  async putAgents(accessToken: string, id: string, agents: CloudAgent[]): Promise<void> {
    await request(this.#url(`v1/servers/${segment(id)}/agents`), { method: "PUT", bearer: accessToken, json: { agents } }, this.#fetch)
  }

  /** A token for one server. `audience` is the server's URL; it is sent in its canonical form. */
  serverToken(accessToken: string, audience: string): Promise<{ token: string; audience: string; expires_at: number }> {
    return json(this.#url("v1/server-tokens"), { method: "POST", bearer: accessToken, json: { audience: canonicalOrSame(audience) } }, this.#fetch)
  }

  #url(path: string): string {
    return `${this.#base}/${path}`
  }
}
