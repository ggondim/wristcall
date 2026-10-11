import { canonical } from "../lib/address"
import { ApiError } from "../lib/errors"
import { DEFAULT_TIMEOUT_MS, download, json } from "../lib/http"
import { REQUEST_ID } from "../lib/ids"

export interface Health {
  status: string
  version: string
  protocol: number
  account: { issuer: string; device_credential: string } | null
  push: { relay: string } | null
}
export interface AgentDetail {
  id: string
  slug: string
  display_name: string
  icon: string
  call_type: "conversation" | "one-shot" | "monologue"
  [field: string]: unknown
}
export interface Provider {
  name: string
  kind: "stt" | "action" | "tts" | "webhook"
}
export interface DeviceRecord {
  id: string
  name: string
  created_at: string | number
}
export interface PairingCodeGrant {
  code: string
  expires_at: number
  server_url: string
  via_directory: boolean
  warning?: string
}
export interface ApprovalRequest {
  request_id: string
  device_name: string
  expires_at: number
}
export interface CallEntry {
  role: "user" | "agent" | string
  text?: string | null
  /** Why the text is missing or partial: `stt_failed`, `responder_failed`, `tts_failed`, `unreadable`. */
  error?: string | null
  at?: number
}

export interface CallAgent {
  id: string
  slug: string
  display_name: string
}

export interface CallRecord {
  id: string
  status: string
  agent_id?: string | null
  /** `conversation`, `one-shot` or `monologue`. */
  call_type?: string
  error?: string | null
  text?: string | null
  attempts?: number
  last_http_status?: number | null
  /** Unix seconds. */
  created_at?: number
  /** Unix seconds; null keeps the call until the user deletes it. */
  expires_at?: number | null
  agent?: CallAgent | null
  entries?: CallEntry[]
  [field: string]: unknown
}
export interface CallsPage {
  calls: CallRecord[]
  next_before: string | null
}
export interface CallsQuery {
  agent?: string
  q?: string
  /** Unix seconds. */
  since?: number
  /** Unix seconds, excluded. */
  until?: number
  limit?: number
  before?: string
}

const HISTORY_TIMEOUT_MS = 30_000
const MAX_LIMIT = 100
const DEFAULT_LIMIT = 50
const MAX_TEXT = 500

/**
 * Management routes of one server (`docs/protocol.md`, "Management API" and "History"), with a personal token.
 * The token lives in a private field, so it is in no `JSON.stringify`, log or error.
 */
export class ServerClient {
  readonly #base: string
  readonly #token: string | null
  readonly #fetch: typeof fetch | undefined

  constructor(base: URL, token: string | null, fetchImpl?: typeof fetch) {
    this.#base = canonical(base)
    this.#token = token
    this.#fetch = fetchImpl
  }

  // Health and account link by code carry no personal token.
  health(): Promise<Health> {
    return json<Health>(this.#url("health"), {}, this.#fetch)
  }

  async providers(): Promise<{ providers: Provider[]; custom_endpoints: boolean }> {
    return this.#get("providers")
  }

  async agents(): Promise<AgentDetail[]> {
    return (await this.#get<{ agents: AgentDetail[] }>("agents")).agents
  }

  async agent(ref: string): Promise<AgentDetail> {
    return this.#get(`agents/${segment(ref)}`)
  }

  createAgent(fields: Record<string, unknown>): Promise<AgentDetail> {
    return this.#send("agents", "POST", fields)
  }

  async updateAgent(ref: string, fields: Record<string, unknown>): Promise<AgentDetail> {
    return this.#send(`agents/${segment(ref)}`, "PATCH", fields)
  }

  async deleteAgent(ref: string): Promise<void> {
    await this.#send(`agents/${segment(ref)}`, "DELETE")
  }

  async devices(): Promise<DeviceRecord[]> {
    return (await this.#get<{ devices: DeviceRecord[] }>("devices")).devices
  }

  async revokeDevice(id: string): Promise<void> {
    await this.#send(`devices/${segment(id)}`, "DELETE")
  }

  createPairingCode(): Promise<PairingCodeGrant> {
    return this.#send("pairing-codes", "POST")
  }

  async pairingRequests(): Promise<ApprovalRequest[]> {
    return (await this.#get<{ requests: ApprovalRequest[] }>("pairing-requests")).requests
  }

  async approve(requestId: string): Promise<{ device_name: string }> {
    return this.#send(`pairing-requests/${requestSegment(requestId)}/approve`, "POST")
  }

  async deny(requestId: string): Promise<void> {
    await this.#send(`pairing-requests/${requestSegment(requestId)}/deny`, "POST")
  }

  /** Links the user of this token to the account behind `serverToken` (a per-server token from the Cloud). */
  linkWithToken(serverToken: string): Promise<{ linked: true; issuer: string }> {
    return this.#send("account/link", "POST", { token: serverToken })
  }

  /** Links with a pairing code and no personal token; the reply carries a new personal token. */
  linkWithCode(
    serverToken: string,
    code: string,
  ): Promise<{ linked: true; issuer: string; user: { id: string; handle: string }; api_token: string }> {
    return json(this.#url("account/link"), { method: "POST", json: { token: serverToken, code } }, this.#fetch)
  }

  async unlink(): Promise<void> {
    await this.#send("account/link", "DELETE")
  }

  async setPushKey(pushKey: string): Promise<void> {
    await this.#send("push", "PUT", { push_key: pushKey })
  }

  async clearPushKey(): Promise<void> {
    await this.#send("push", "DELETE")
  }

  calls(query: CallsQuery): Promise<CallsPage> {
    const params = new URLSearchParams()
    appendFilters(params, query)
    const q = query.q?.trim()
    if (q) params.set("q", Array.from(q).slice(0, MAX_TEXT).join(""))
    if (query.before) params.set("before", query.before)
    // The default is the server's documented one (docs/protocol.md, History: 1 to 100, default 50).
    const wanted = query.limit !== undefined && Number.isFinite(query.limit) ? Math.trunc(query.limit) : DEFAULT_LIMIT
    const limit = Math.min(Math.max(wanted, 1), MAX_LIMIT)
    params.set("limit", String(limit))
    return this.#send("calls", "GET", undefined, params, HISTORY_TIMEOUT_MS)
  }

  async call(id: string): Promise<CallRecord> {
    return this.#send(`calls/${segment(id)}`, "GET", undefined, undefined, HISTORY_TIMEOUT_MS)
  }

  async deleteCall(id: string): Promise<void> {
    await this.#send(`calls/${segment(id)}`, "DELETE", undefined, undefined, HISTORY_TIMEOUT_MS)
  }

  /** `DELETE /v1/calls?agent=` or `?all=true`; the number of calls deleted. A blank agent is refused here (it would read as "all"). */
  async deleteCalls(q: { agent: string } | { all: true }): Promise<number> {
    const params = new URLSearchParams()
    if ("all" in q) {
      params.set("all", "true")
    } else {
      const agent = q.agent.trim()
      if (agent === "") throw new Error("Choose an agent.")
      params.set("agent", agent)
    }
    const reply = await this.#send<{ deleted?: unknown }>("calls", "DELETE", undefined, params, HISTORY_TIMEOUT_MS)
    const deleted = reply?.deleted
    return typeof deleted === "number" && Number.isFinite(deleted) ? deleted : 0
  }

  async redeliver(id: string): Promise<CallRecord> {
    return this.#send(`calls/${segment(id)}/redeliver`, "POST", undefined, undefined, HISTORY_TIMEOUT_MS)
  }

  async exportCalls(
    format: "md" | "json",
    query: Omit<CallsQuery, "q" | "limit" | "before">,
  ): Promise<{ blob: Blob; filename: string }> {
    const params = new URLSearchParams({ format })
    appendFilters(params, query)
    const { blob, headers } = await download(
      this.#url("calls/export", params),
      { bearer: this.#bearer(), timeoutMs: HISTORY_TIMEOUT_MS },
      this.#fetch,
    )
    return { blob, filename: filenameFrom(headers.get("content-disposition"), `wristcall-calls.${format}`) }
  }

  #url(path: string, params?: URLSearchParams): string {
    // `URLSearchParams.size` is missing before Safari 17: use the string.
    const qs = params?.toString() ?? ""
    return `${this.#base}/v1/${path}${qs === "" ? "" : `?${qs}`}`
  }

  #bearer(): string {
    if (this.#token === null || this.#token === "") throw new ApiError(401, "unauthorized", "This server has no token.")
    return this.#token
  }

  #get<T>(path: string): Promise<T> {
    return this.#send(path, "GET")
  }

  async #send<T>(
    path: string,
    method: string,
    body?: unknown,
    params?: URLSearchParams,
    timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ): Promise<T> {
    return json<T>(this.#url(path, params), { method, bearer: this.#bearer(), json: body, timeoutMs }, this.#fetch)
  }
}

/** One path segment: `/`, `?` and `#` are escaped, and `.`/`..` (which a URL would resolve) are refused. */
function segment(id: string): string {
  if (id === "" || id === "." || id === "..") throw new Error("Invalid identifier.")
  return encodeURIComponent(id)
}

function requestSegment(id: string): string {
  if (!REQUEST_ID.test(id)) throw new Error("Invalid request code.")
  return id
}

function appendFilters(params: URLSearchParams, query: Pick<CallsQuery, "agent" | "since" | "until">): void {
  const agent = query.agent?.trim()
  if (agent) params.set("agent", agent)
  if (query.since !== undefined && Number.isFinite(query.since)) params.set("since", String(Math.floor(query.since)))
  if (query.until !== undefined && Number.isFinite(query.until)) params.set("until", String(Math.floor(query.until)))
}

/** `filename` of a Content-Disposition header as a safe file name: last path part, `[A-Za-z0-9._-]`, no leading dot. */
function filenameFrom(header: string | null, fallback: string): string {
  const match = header === null ? null : /(?:^|[;\s])filename=(?:"([^"]*)"|([^;\s]*))/i.exec(header)
  const raw = match?.[1] ?? match?.[2]
  if (raw === undefined) return fallback
  const last = raw.split(/[/\\]/).pop() ?? ""
  const clean = last.replace(/[^A-Za-z0-9._-]/g, "").replace(/^\.+/, "").slice(0, 128)
  return clean === "" ? fallback : clean
}
