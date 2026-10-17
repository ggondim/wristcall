import type { AgentDetail, Provider, ServerClient } from "../api/server"
import { userMessage } from "../lib/errors"
import { type AgentColorName, resolvedColor } from "../ui/agentColor"
import { BaseModel } from "../ui/useModel"

// Same rules, fields and texts as `watch/Phone/AgentFormModel.swift`, so a refusal shows up before anything is sent.

/** What a step of the call is set to: nothing chosen, one of the operator's providers, or the user's own service. */
export type Choice = "none" | "custom" | `provider:${string}`
/** Days an agent's calls stay in the history: the server's default, forever, or a number of days. */
export type Retention = "default" | "forever" | number
/** The three steps of a call, and the agent fields they fill. */
export type Stage = "stt" | "action" | "tts"

export const STAGES: readonly Stage[] = ["stt", "action", "tts"]
export const CALL_TYPES = ["conversation", "one-shot", "monologue"] as const
/** Text of a call type for screens (an unknown one shows as it came). */
export function callTypeLabel(type: string): string {
  switch (type) {
    case "conversation":
      return "Conversation"
    case "one-shot":
      return "One-shot"
    case "monologue":
      return "Monologue"
    default:
      return type
  }
}

export const DEFAULT_SILENCE_MS = 800
export const MIN_SILENCE_MS = 100
export const MAX_SILENCE_MS = 10_000
export const MAX_RETENTION_DAYS = 36_500

const SLUG = /^[a-z0-9][a-z0-9-]{0,31}$/
const ICON_NAME = /^[a-z0-9]+(\.[a-z0-9]+)*$/
const HEADER_NAME = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/
const PRINTABLE_ASCII = /^[\x20-\x7e]*$/
const RESERVED_HEADERS = new Set([
  "content-type", "content-length", "host", "user-agent", "idempotency-key", "transfer-encoding", "connection", "expect",
])

export interface FormValues {
  slug: string
  displayName: string
  icon: string
  /** The agent's color; only sent when the server takes colors (`colorSupported`). */
  color: AgentColorName
  callType: string
  turnEnd: string
  language: string
  systemPrompt: string
  fallbackMessage: string
  stt: Choice
  action: Choice
  tts: Choice
  /** One-way agents' own webhook: `{"type":"webhook","url":...}` plus at most one header. */
  webhookUrl: string
  webhookHeaderName: string
  webhookHeaderValue: string
  /** A conversation's own chat endpoint: `{"type":"openai_chat",...}`. */
  chatBaseUrl: string
  chatModel: string
  chatApiKey: string
  /** Own speech to text (`openai_stt`) and voice (`openai_tts`) services. */
  sttBaseUrl: string
  sttModel: string
  sttApiKey: string
  ttsBaseUrl: string
  ttsModel: string
  ttsApiKey: string
  /** `vad.silence_ms`, 100 to 10000. */
  silenceMs: number
  retention: Retention
}

export interface FormState extends FormValues {
  saving: boolean
  /** Whether the server takes `color` (0.8.1 and later); the picker hides and nothing is sent without it. */
  colorSupported: boolean
}

export interface FormOptions {
  /** Whether the server accepts the user's own endpoints. */
  customEndpoints?: boolean
  /** Whether the server takes agent colors (see `supportsAgentColor`). */
  colorSupported?: boolean
  /** Runs after a successful save (the list is read again). */
  onSaved?: () => Promise<void> | void
}

type Json = Record<string, unknown>

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((item, i) => deepEqual(item, b[i]))
  }
  if (isRecord(a) && isRecord(b)) {
    const keys = Object.keys(a)
    return keys.length === Object.keys(b).length && keys.every((key) => key in b && deepEqual(a[key], b[key]))
  }
  return false
}

function choiceOf(value: unknown): Choice {
  if (!isRecord(value)) return "none"
  if (typeof value.provider === "string") return `provider:${value.provider}`
  return "type" in value ? "custom" : "none"
}

function silenceOf(agent: AgentDetail): number {
  const vad = agent.vad
  const silence = isRecord(vad) ? vad.silence_ms : undefined
  return typeof silence === "number" && Number.isFinite(silence) ? Math.trunc(silence) : DEFAULT_SILENCE_MS
}

function retentionOf(agent: AgentDetail): Retention {
  const days = agent.retention_days
  if (typeof days === "number" && Number.isInteger(days)) return days
  return days === "forever" ? "forever" : "default"
}

function encodeRetention(retention: Retention): unknown {
  return retention === "default" ? null : retention
}

/** An http(s) address with a host and without credentials, query or fragment. */
function isHttpUrl(value: string, httpsOnly: boolean): boolean {
  if (/[?#]/.test(value)) return false
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return false
  }
  const scheme = url.protocol
  if (scheme !== "https:" && (httpsOnly || scheme !== "http:")) return false
  return url.hostname !== "" && url.username === "" && url.password === ""
}

/** `my-notes` out of `My Notes!`: fills the slug field until the user types their own. */
export function suggestSlug(name: string): string {
  const folded = name.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase()
  let out = ""
  for (const char of folded) {
    if (/^[a-z0-9]$/.test(char)) out += char
    else if (out !== "" && !out.endsWith("-")) out += "-"
  }
  return out.slice(0, 32).replace(/-+$/, "")
}

/** The form values of an agent, and the one webhook header it shows. A new agent starts empty. */
function read(agent: AgentDetail | null): { values: FormValues; shownHeader: string } {
  const values: FormValues = {
    slug: "",
    displayName: "",
    icon: "waveform",
    color: "mint",
    callType: "conversation",
    turnEnd: "auto",
    language: "en",
    systemPrompt: "",
    fallbackMessage: "",
    stt: "none",
    action: "none",
    tts: "none",
    webhookUrl: "",
    webhookHeaderName: "",
    webhookHeaderValue: "",
    chatBaseUrl: "",
    chatModel: "",
    chatApiKey: "",
    sttBaseUrl: "",
    sttModel: "",
    sttApiKey: "",
    ttsBaseUrl: "",
    ttsModel: "",
    ttsApiKey: "",
    silenceMs: DEFAULT_SILENCE_MS,
    retention: "default",
  }
  let shownHeader = ""
  if (agent === null) return { values, shownHeader }
  values.slug = agent.slug
  values.displayName = agent.display_name
  values.icon = agent.icon
  values.color = resolvedColor(agent.color)
  values.callType = agent.call_type
  values.turnEnd = text(agent.turn_end, "auto")
  values.language = text(agent.language, "en")
  values.systemPrompt = text(agent.system_prompt)
  values.fallbackMessage = text(agent.fallback_message)
  values.stt = choiceOf(agent.stt)
  values.action = choiceOf(agent.action)
  values.tts = choiceOf(agent.tts)
  values.silenceMs = silenceOf(agent)
  values.retention = retentionOf(agent)
  for (const stage of STAGES) {
    const endpoint = agent[stage]
    if (!isRecord(endpoint) || "provider" in endpoint || typeof endpoint.type !== "string") continue
    const url = text(endpoint.base_url)
    const model = text(endpoint.model)
    const key = text(endpoint.api_key)
    if (stage === "action" && endpoint.type === "webhook") {
      values.webhookUrl = text(endpoint.url)
      const headers = endpoint.headers
      const first = isRecord(headers) ? Object.keys(headers).sort()[0] : undefined
      if (first !== undefined && isRecord(headers)) {
        shownHeader = first
        values.webhookHeaderName = first
        values.webhookHeaderValue = text(headers[first])
      }
    } else if (stage === "action" && endpoint.type === "openai_chat") {
      Object.assign(values, { chatBaseUrl: url, chatModel: model, chatApiKey: key })
    } else if (stage === "stt" && endpoint.type === "openai_stt") {
      Object.assign(values, { sttBaseUrl: url, sttModel: model, sttApiKey: key })
    } else if (stage === "tts" && endpoint.type === "openai_tts") {
      Object.assign(values, { ttsBaseUrl: url, ttsModel: model, ttsApiKey: key })
    }
  }
  return { values, shownHeader }
}

/**
 * State of the create/edit form of an agent. `fields` is the request body: on creation everything the user filled
 * in, on an edit only what differs from the stored agent. Secrets the server masks (`***`) stay as they came: the
 * server keeps the stored value when it gets the mask back.
 */
export class AgentFormModel extends BaseModel<FormState> {
  readonly existing: AgentDetail | null
  readonly providers: readonly Provider[]
  readonly customEndpoints: boolean
  readonly #client: ServerClient
  readonly #onSaved: FormOptions["onSaved"]
  /** Endpoints as the form builds them right after loading; what `fields` compares with. */
  readonly #initialEndpoints: Partial<Record<Stage, Json>> = {}
  /** The one header the form shows when the stored webhook has several. */
  readonly #shownHeader: string
  #slugEdited = false

  constructor(client: ServerClient, existing: AgentDetail | null, providers: readonly Provider[], options: FormOptions = {}) {
    super({ ...read(existing).values, saving: false, colorSupported: options.colorSupported ?? false })
    this.#client = client
    this.existing = existing
    this.providers = providers
    this.customEndpoints = options.customEndpoints ?? false
    this.#onSaved = options.onSaved
    this.#shownHeader = read(existing).shownHeader
    if (existing !== null) {
      for (const stage of STAGES) {
        const built = this.#endpoint(stage)
        if (built !== null) this.#initialEndpoints[stage] = built
      }
    }
  }

  get isEditing(): boolean {
    return this.existing !== null
  }

  get isOneWay(): boolean {
    return this.state.callType !== "conversation"
  }

  /** The request body (see the class comment). */
  get fields(): Record<string, unknown> {
    return this.#body()
  }

  /** The server's version arrived (or changed) after the form was built. */
  setColorSupported(colorSupported: boolean): void {
    if (colorSupported !== this.state.colorSupported) this.set({ colorSupported })
  }

  /** Changes fields. A new agent's slug follows the name until the user types a slug. */
  update(patch: Partial<FormValues>): void {
    const next = { ...patch }
    if (next.slug !== undefined) this.#slugEdited = true
    else if (next.displayName !== undefined && this.existing === null && !this.#slugEdited) next.slug = suggestSlug(next.displayName)
    this.set(next)
  }

  /** Changing the call type drops a provider choice that does not fit the new one. */
  selectCallType(type: string): void {
    this.set({ callType: type })
    const action = this.state.action
    if (action.startsWith("provider:") && !this.#providerNames("action").includes(action.slice("provider:".length))) {
      this.set({ action: "none" })
    }
  }

  /** What the picker of a step offers. "Custom" only when the server allows it (or the agent already holds one). */
  choices(stage: Stage): Choice[] {
    const current = this.state[stage]
    const list: Choice[] = []
    if (!this.isEditing || current === "none") list.push("none")
    const names = this.#providerNames(stage)
    for (const name of names) list.push(`provider:${name}`)
    if (current.startsWith("provider:") && !names.includes(current.slice("provider:".length))) list.push(current)
    if (this.customEndpoints || current === "custom") list.push("custom")
    return list
  }

  /** Label of a step's picker option. */
  label(choice: Choice): string {
    if (choice === "none") return this.isEditing ? "Choose…" : "Server default"
    if (choice === "custom") return "Custom"
    return choice.slice("provider:".length)
  }

  /** Why the form cannot be sent yet, or `null`. */
  validate(): string | null {
    const s = this.state
    const name = s.displayName.trim()
    if (!this.isEditing && !SLUG.test(s.slug)) {
      return "Slug: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit."
    }
    if (name === "" || Array.from(name).length > 64) return "Enter a name of up to 64 characters."
    if (!ICON_NAME.test(s.icon)) return "Pick an icon."
    if (!(CALL_TYPES as readonly string[]).includes(s.callType)) return "Pick a call type."
    for (const stage of this.#activeStages()) {
      const message = this.#validateStage(stage)
      if (message !== null) return message
    }
    const languageLength = Array.from(s.language.trim()).length
    if (languageLength < 2 || languageLength > 16) return "Language: use 2 to 16 characters, for example en or pt-BR."
    if (Array.from(s.systemPrompt).length > 20_000) return "The prompt can have up to 20000 characters."
    const fallbackLength = Array.from(s.fallbackMessage).length
    if (fallbackLength > 500 || (this.isEditing && fallbackLength === 0)) return "The failure message needs 1 to 500 characters."
    if (!Number.isInteger(s.silenceMs) || s.silenceMs < MIN_SILENCE_MS || s.silenceMs > MAX_SILENCE_MS) {
      return "Silence must be between 100 and 10000 ms."
    }
    if (typeof s.retention === "number" && (!Number.isInteger(s.retention) || s.retention < 1 || s.retention > MAX_RETENTION_DAYS)) {
      return "Keep history for 1 to 36500 days."
    }
    return null
  }

  /** True when an edit changed nothing (saving would send nothing). */
  get unchanged(): boolean {
    return this.existing !== null && Object.keys(this.#body()).length === 0
  }

  /**
   * Creates the agent (`POST`) or updates it (`PATCH`, only the changed fields). Throws an `Error` whose message is
   * text for the screen. An edit that changed nothing sends nothing.
   */
  async save(): Promise<AgentDetail> {
    if (this.state.saving) throw new Error("Saving…")
    const message = this.validate()
    if (message !== null) throw new Error(message)
    const body = this.#body()
    this.set({ saving: true })
    try {
      let saved: AgentDetail
      if (this.existing === null) saved = await this.#client.createAgent(body)
      else if (Object.keys(body).length === 0) saved = this.existing
      else saved = await this.#client.updateAgent(this.existing.id, body)
      await this.#onSaved?.()
      return saved
    } catch (e) {
      throw new Error(userMessage(e))
    } finally {
      this.set({ saving: false })
    }
  }

  // Pickers

  /** The kind of provider (in `GET /v1/providers`) that fills a step for the current call type. */
  #kind(stage: Stage): string {
    if (stage === "action") return this.isOneWay ? "webhook" : "action"
    return stage
  }

  #providerNames(stage: Stage): string[] {
    return this.providers.filter((provider) => provider.kind === this.#kind(stage)).map((provider) => provider.name)
  }

  /** The steps the current call type uses (a one-way agent has no voice). */
  #activeStages(): Stage[] {
    return this.isOneWay ? ["stt", "action"] : [...STAGES]
  }

  // Endpoints

  #customType(stage: Stage): string {
    if (stage === "stt") return "openai_stt"
    if (stage === "tts") return "openai_tts"
    return this.isOneWay ? "webhook" : "openai_chat"
  }

  /** The stored endpoint, when it is a custom one of the type this step has now (its other options and masked secrets are kept). */
  #storedCustom(stage: Stage): Json | null {
    const stored = this.existing?.[stage]
    if (!isRecord(stored) || "provider" in stored || stored.type !== this.#customType(stage)) return null
    return stored
  }

  #openAiFields(stage: Stage): [url: string, model: string, key: string] {
    const s = this.state
    if (stage === "stt") return [s.sttBaseUrl, s.sttModel, s.sttApiKey]
    if (stage === "action") return [s.chatBaseUrl, s.chatModel, s.chatApiKey]
    return [s.ttsBaseUrl, s.ttsModel, s.ttsApiKey]
  }

  #endpoint(stage: Stage): Json | null {
    const choice = this.state[stage]
    if (choice === "none") return null
    if (choice === "custom") return this.#customEndpoint(stage)
    return { provider: choice.slice("provider:".length) }
  }

  #customEndpoint(stage: Stage): Json {
    const s = this.state
    const stored = this.#storedCustom(stage)
    const object: Json = stored === null ? { type: this.#customType(stage) } : { ...stored }
    if (stage === "action" && this.isOneWay) {
      object.url = s.webhookUrl.trim()
      const headers: Json = isRecord(object.headers) ? { ...object.headers } : {}
      delete headers[this.#shownHeader]
      const name = s.webhookHeaderName.trim()
      if (name !== "") headers[name] = s.webhookHeaderValue
      if (Object.keys(headers).length === 0) delete object.headers
      else object.headers = headers
    } else {
      const [url, model, key] = this.#openAiFields(stage)
      object.base_url = url.trim()
      object.model = model.trim()
      if (key === "") delete object.api_key
      else object.api_key = key
    }
    return object
  }

  /**
   * True on an edit when the step still holds what the server stores: it is not sent, and not asked for again (a
   * secret the server masks cannot be checked here). Not so when the call type changed to or from a webhook.
   */
  #isUntouched(stage: Stage, built: Json | null): boolean {
    const existing = this.existing
    if (existing === null || built === null || !deepEqual(built, this.#initialEndpoints[stage])) return false
    return !(stage === "action" && this.isOneWay !== (existing.call_type !== "conversation"))
  }

  // Validation

  #label(stage: Stage): string {
    if (stage === "stt") return "Speech to text"
    if (stage === "tts") return "Voice"
    return this.isOneWay ? "Webhook" : "Responder"
  }

  #validateStage(stage: Stage): string | null {
    const s = this.state
    const selected = s[stage]
    const label = this.#label(stage)
    if (selected === "none") return this.isEditing ? `Choose ${label}.` : null
    // Untouched on an edit: the server already holds it, and it is not sent.
    if (this.#isUntouched(stage, this.#endpoint(stage))) return null
    if (selected !== "custom") {
      const name = selected.slice("provider:".length)
      return this.#providerNames(stage).includes(name) ? null : `${label}: this server has no "${name}" for this step.`
    }
    if (!this.customEndpoints) return `${label}: this server doesn't accept custom endpoints.`
    const sameType = this.#storedCustom(stage) !== null
    const masked = (secret: string): boolean => secret === "***" && !sameType
    const reenter = `${label}: enter the secret again, a stored one only carries over to the same kind of endpoint.`
    if (stage === "action" && this.isOneWay) {
      if (!isHttpUrl(s.webhookUrl.trim(), true)) return "Webhook: enter an https:// address without credentials, query or fragment."
      const header = s.webhookHeaderName.trim()
      if ((header === "") !== (s.webhookHeaderValue === "")) return "Webhook: a header needs a name and a value."
      if (header !== "") {
        if (!HEADER_NAME.test(header) || RESERVED_HEADERS.has(header.toLowerCase())) {
          return "Webhook: that header name is invalid or set by the server."
        }
        const value = s.webhookHeaderValue
        if (!PRINTABLE_ASCII.test(value) || value.length > 4096) return "Webhook: the header value must be printable ASCII on one line."
        if (masked(value)) return reenter
        // A stored secret only carries over under the name it was stored with.
        if (value === "***" && header !== this.#shownHeader) return reenter
      }
      return null
    }
    const [url, model, key] = this.#openAiFields(stage)
    if (!isHttpUrl(url.trim(), false)) return `${label}: enter the base URL (http:// or https://, no credentials or query).`
    if (model.trim() === "") return `${label}: enter the model name.`
    return masked(key) ? reenter : null
  }

  // Body

  #body(): Json {
    const s = this.state
    const out: Json = {}
    const name = s.displayName.trim()
    const language = s.language.trim()
    const agent = this.existing
    if (agent !== null) {
      if (name !== agent.display_name) out.display_name = name
      if (s.icon !== agent.icon) out.icon = s.icon
      if (s.colorSupported && s.color !== resolvedColor(agent.color)) out.color = s.color
      if (s.callType !== agent.call_type) out.call_type = s.callType
      if (s.turnEnd !== text(agent.turn_end, "auto")) out.turn_end = s.turnEnd
      if (language !== text(agent.language, "en")) out.language = language
      if (s.systemPrompt !== text(agent.system_prompt)) out.system_prompt = s.systemPrompt
      if (s.fallbackMessage !== text(agent.fallback_message)) out.fallback_message = s.fallbackMessage
      for (const stage of this.#activeStages()) {
        const built = this.#endpoint(stage)
        if (built !== null && !this.#isUntouched(stage, built)) out[stage] = built
      }
      // Switching to one-way drops the stored voice (the server would keep it otherwise); an agent that is already
      // one-way keeps whatever it holds.
      if (this.isOneWay && agent.call_type === "conversation" && this.#initialEndpoints.tts !== undefined) out.tts = null
      if (s.silenceMs !== silenceOf(agent)) out.vad = { silence_ms: s.silenceMs }
      if (s.retention !== retentionOf(agent)) out.retention_days = encodeRetention(s.retention)
      return out
    }
    out.slug = s.slug
    out.display_name = name
    out.icon = s.icon
    if (s.colorSupported) out.color = s.color
    out.call_type = s.callType
    out.turn_end = s.turnEnd
    if (language !== "") out.language = language
    if (s.systemPrompt !== "") out.system_prompt = s.systemPrompt
    if (s.fallbackMessage !== "") out.fallback_message = s.fallbackMessage
    for (const stage of this.#activeStages()) {
      const built = this.#endpoint(stage)
      if (built !== null) out[stage] = built
    }
    if (s.silenceMs !== DEFAULT_SILENCE_MS) out.vad = { silence_ms: s.silenceMs }
    if (s.retention !== "default") out.retention_days = encodeRetention(s.retention)
    return out
  }
}
