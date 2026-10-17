import { describe, expect, test } from "vitest"
import { ServerClient, type AgentDetail, type Provider } from "../api/server"
import { mockFetch, type MockFetch } from "../test/mockFetch"
import { AgentFormModel, suggestSlug } from "./agentForm"
import { AgentsModel } from "./agents"

const BASE = new URL("https://s.example")
const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
const PROVIDERS: Provider[] = [
  { name: "whisper", kind: "stt" },
  { name: "claude", kind: "action" },
  { name: "piper", kind: "tts" },
  { name: "notes-hook", kind: "webhook" },
]

function agent(over: Partial<AgentDetail> = {}): AgentDetail {
  return {
    id: "ag_a",
    slug: "alpha",
    display_name: "Alpha",
    icon: "waveform",
    call_type: "conversation",
    turn_end: "auto",
    position: 0,
    language: "en",
    stt: { provider: "whisper" },
    action: { provider: "claude" },
    tts: { provider: "piper" },
    system_prompt: "",
    fallback_message: "Sorry, I couldn't answer right now.",
    vad: { silence_ms: 800 },
    timeouts: {},
    retention_days: null,
    ...over,
  }
}

/** A server that holds `agents` and answers the routes the models use. */
function fakeServer(agents: AgentDetail[], fail: (method: string, url: string) => Response | null = () => null): MockFetch {
  return mockFetch((url, init) => {
    const failure = fail(init.method ?? "GET", url)
    if (failure !== null) return failure
    if (url.endsWith("/v1/providers")) return ok({ providers: PROVIDERS, custom_endpoints: true })
    if (url.endsWith("/v1/agents") && init.method === "POST") return ok({ ...agent(), ...JSON.parse(init.body as string) }, 201)
    if (url.endsWith("/v1/agents")) return ok({ agents })
    if (init.method === "DELETE") return new Response(null, { status: 204 })
    if (init.method === "PATCH") {
      const id = decodeURIComponent(url.split("/").pop()!)
      const body = JSON.parse(init.body as string) as Record<string, unknown>
      const found = agents.find((candidate) => candidate.id === id)!
      if (typeof body.position === "number") {
        agents.splice(agents.indexOf(found), 1)
        agents.splice(body.position, 0, found)
        agents.forEach((candidate, index) => (candidate.position = index))
      }
      return ok(found)
    }
    return new Response("{}", { status: 404 })
  })
}

const client = (fetchMock: MockFetch) => new ServerClient(BASE, "wc_pat_x", fetchMock)
const sent = (fetchMock: MockFetch, method: string) => fetchMock.calls.filter((call) => call.init.method === method)

describe("AgentsModel", () => {
  test("loadSortsByPositionAndReadsProviders", async () => {
    const fetchMock = fakeServer([agent({ id: "ag_b", position: 1 }), agent({ id: "ag_a", position: 0 })])
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_a", "ag_b"])
    expect(model.state.providers).toEqual(PROVIDERS)
    expect(model.state.customEndpoints).toBe(true)
    expect(model.state.loaded && model.state.providersLoaded).toBe(true)
    expect(model.state.error).toBeNull()
  })

  test("moveSendsOnePatch", async () => {
    const fetchMock = fakeServer([agent({ id: "ag_a" }), agent({ id: "ag_b", position: 1 }), agent({ id: "ag_c", position: 2 })])
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    await model.move("ag_c", -1)
    const patches = sent(fetchMock, "PATCH")
    expect(patches).toHaveLength(1)
    expect(patches[0]!.url).toBe("https://s.example/v1/agents/ag_c")
    expect(JSON.parse(patches[0]!.init.body as string)).toEqual({ position: 1 })
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_a", "ag_c", "ag_b"])
    // The edges do nothing and send nothing.
    await model.move("ag_a", -1)
    await model.move("ag_b", 1)
    await model.move("ag_unknown", 1)
    expect(sent(fetchMock, "PATCH")).toHaveLength(1)
  })

  test("moveReloadsAndShowsTheErrorWhenRefused", async () => {
    const list = [agent({ id: "ag_a" }), agent({ id: "ag_b", position: 1 })]
    const fetchMock = fakeServer(list, (method) => (method === "PATCH" ? ok({ error: "invalid", message: "position: out of range" }, 400) : null))
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    await model.move("ag_b", -1)
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_a", "ag_b"])
    expect(model.state.error).toBe("position: out of range")
  })

  test("removeDeletesAndNotifiesListeners", async () => {
    const fetchMock = fakeServer([agent({ id: "ag_a" }), agent({ id: "ag_b", position: 1 })])
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    const seen: string[][] = []
    const off = model.onAgentsChanged((serverId, agents) => seen.push([serverId, ...agents.map((a) => a.id)]))
    model.onAgentsChanged(() => {
      throw new Error("a broken listener")
    })
    await model.remove("ag_a")
    expect(sent(fetchMock, "DELETE")[0]!.url).toBe("https://s.example/v1/agents/ag_a")
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_b"])
    expect(seen).toEqual([["home", "ag_b"]])
    off()
    await model.load()
    expect(seen).toHaveLength(1)
  })

  test("removeReloadsOnError", async () => {
    const list = [agent({ id: "ag_a" }), agent({ id: "ag_b", position: 1 })]
    const fetchMock = fakeServer(list, (method) => {
      if (method !== "DELETE") return null
      list.pop() // gone on the server meanwhile
      return ok({ error: "not_found", message: "agent not found: ag_b" }, 404)
    })
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    await model.remove("ag_b")
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_a"])
    expect(model.state.error).toBe("Not found.")
    expect(sent(fetchMock, "GET").filter((call) => call.url.endsWith("/v1/agents"))).toHaveLength(2)
  })

  test("aReadStartedBeforeADeleteDoesNotBringTheAgentBack", async () => {
    const list = [agent({ id: "ag_a" }), agent({ id: "ag_b", position: 1 })]
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    let slow = false
    const fetchMock = mockFetch(async (url, init) => {
      if (init.method === "DELETE") return new Response(null, { status: 204 })
      if (url.endsWith("/v1/agents")) {
        const answer = ok({ agents: [...list] }) // the list as it was when the read started
        if (slow) await gate
        return answer
      }
      return ok({ providers: PROVIDERS, custom_endpoints: false })
    })
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    slow = true
    const reading = model.load()
    await model.remove("ag_a")
    release()
    await reading
    expect(model.state.agents.map((a) => a.id)).toEqual(["ag_b"])
  })

  test("aFailedLoadKeepsTheListAndShowsTheError", async () => {
    let down = false
    const fetchMock = fakeServer([agent()], () => (down ? ok({ error: "x", message: "The server is unavailable." }, 503) : null))
    const model = new AgentsModel(client(fetchMock), "home")
    await model.load()
    down = true
    await model.load()
    expect(model.state.agents).toHaveLength(1)
    expect(model.state.error).toBe("The server is unavailable.")
  })
})

describe("AgentFormModel", () => {
  const form = (existing: AgentDetail | null, fetchMock = fakeServer([]), customEndpoints = true) =>
    new AgentFormModel(client(fetchMock), existing, PROVIDERS, { customEndpoints })
  const withColor = (existing: AgentDetail | null, colorSupported = true) =>
    new AgentFormModel(client(fakeServer([])), existing, PROVIDERS, { customEndpoints: true, colorSupported })
  const own = { type: "openai_stt", base_url: "https://stt.example/v1", model: "whisper-1", api_key: "***" }

  test("new agent defaults mint", () => {
    const f = withColor(null)
    expect(f.state.color).toBe("mint")
    f.update({ displayName: "Notes" })
    expect(f.fields.color).toBe("mint")
  })

  test("editing keeps agent color", () => {
    expect(withColor(agent({ color: "purple" })).state.color).toBe("purple")
    // Missing or unknown (a newer server's) shows as mint.
    expect(withColor(agent()).state.color).toBe("mint")
    expect(withColor(agent({ color: "chartreuse" })).state.color).toBe("mint")
  })

  test("changed color sent", () => {
    const f = withColor(agent({ color: "purple" }))
    f.update({ color: "red" })
    expect(f.fields).toEqual({ color: "red" })
    expect(f.unchanged).toBe(false)
  })

  test("unchanged color not sent", () => {
    const f = withColor(agent({ color: "purple" }))
    f.update({ displayName: "Other" })
    expect(f.fields).toEqual({ display_name: "Other" })
    // An agent without a color shows mint; leaving it there sends nothing.
    expect(withColor(agent()).fields).toEqual({})
    expect(withColor(agent()).unchanged).toBe(true)
  })

  test("color is not sent to a server older than 0.8.1", () => {
    const created = withColor(null, false)
    created.update({ displayName: "Notes", color: "red" })
    expect(created.fields).not.toHaveProperty("color")
    const edited = withColor(agent(), false)
    edited.update({ color: "red" })
    expect(edited.fields).toEqual({})
    edited.setColorSupported(true)
    expect(edited.fields).toEqual({ color: "red" })
  })

  test("formValidatesSlug", () => {
    const f = form(null)
    f.update({ displayName: "Notes" })
    expect(f.state.slug).toBe("notes")
    expect(f.validate()).toBeNull()
    const message = "Slug: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit."
    for (const bad of ["", "-a", "A", "a_b", "a b", "a".repeat(33), "é"]) {
      f.update({ slug: bad })
      expect(f.validate(), bad).toBe(message)
    }
    for (const good of ["a", "0", "a-b-", "a".repeat(32)]) {
      f.update({ slug: good })
      expect(f.validate(), good).toBeNull()
    }
    // Typing a slug stops it following the name.
    f.update({ slug: "mine" })
    f.update({ displayName: "Other" })
    expect(f.state.slug).toBe("mine")
    // The slug of an existing agent is not checked (it cannot change).
    expect(form(agent({ slug: "x".repeat(40) })).validate()).toBeNull()
  })

  test("suggestSlugFollowsTheName", () => {
    expect(suggestSlug("My Notes!")).toBe("my-notes")
    expect(suggestSlug("  Ação rápida  ")).toBe("acao-rapida")
    expect(suggestSlug("x".repeat(40))).toBe("x".repeat(32))
    expect(suggestSlug("!!!")).toBe("")
  })

  test("formValidatesNameAndRanges", () => {
    const f = form(null)
    f.update({ displayName: "A", slug: "a" })
    expect(f.validate()).toBeNull()
    f.update({ displayName: "   " })
    expect(f.validate()).toBe("Enter a name of up to 64 characters.")
    f.update({ displayName: "n".repeat(65) })
    expect(f.validate()).toBe("Enter a name of up to 64 characters.")
    f.update({ displayName: "ok", silenceMs: 99 })
    expect(f.validate()).toBe("Silence must be between 100 and 10000 ms.")
    f.update({ silenceMs: Number.NaN })
    expect(f.validate()).toBe("Silence must be between 100 and 10000 ms.")
    f.update({ silenceMs: 800, retention: 0 })
    expect(f.validate()).toBe("Keep history for 1 to 36500 days.")
    for (const bad of [36_501, 1.5, Number.NaN]) {
      f.update({ retention: bad })
      expect(f.validate(), String(bad)).toBe("Keep history for 1 to 36500 days.")
    }
    for (const good of [1, 36_500, "forever", "default"] as const) {
      f.update({ retention: good })
      expect(f.validate(), String(good)).toBeNull()
    }
    f.update({ language: "e" })
    expect(f.validate()).toBe("Language: use 2 to 16 characters, for example en or pt-BR.")
  })

  test("formSendsIntegerRetention", async () => {
    const fetchMock = fakeServer([])
    const f = form(null, fetchMock)
    f.update({ displayName: "Notes", retention: Number("30") })
    await f.save()
    const body = sent(fetchMock, "POST")[0]!.init.body as string
    expect(body).toContain('"retention_days":30')
    expect(body).not.toContain("30.0")
    expect(JSON.parse(body)).toMatchObject({ slug: "notes", display_name: "Notes", icon: "waveform", call_type: "conversation", turn_end: "auto", language: "en" })
    expect(JSON.parse(body).vad).toBeUndefined()
    f.update({ retention: "forever", silenceMs: 1200 })
    expect(f.fields).toMatchObject({ retention_days: "forever", vad: { silence_ms: 1200 } })
  })

  test("aNewAgentWithServerDefaultsSendsNoEndpoint", () => {
    const f = form(null)
    f.update({ displayName: "Notes", stt: "provider:whisper" })
    expect(f.fields.stt).toEqual({ provider: "whisper" })
    expect("action" in f.fields || "tts" in f.fields).toBe(false)
    f.update({ tts: "provider:nope" })
    expect(f.validate()).toBe('Voice: this server has no "nope" for this step.')
  })

  test("patchSendsOnlyChangedFields", async () => {
    const fetchMock = fakeServer([agent()])
    const f = form(agent(), fetchMock)
    expect(f.fields).toEqual({})
    expect(f.unchanged).toBe(true)
    f.update({ displayName: "  Alpha 2  ", language: "pt-BR", retention: 90, silenceMs: 600 })
    f.update({ stt: "provider:whisper" })
    await f.save()
    const patches = sent(fetchMock, "PATCH")
    expect(patches).toHaveLength(1)
    expect(patches[0]!.url).toBe("https://s.example/v1/agents/ag_a")
    expect(JSON.parse(patches[0]!.init.body as string)).toEqual({ display_name: "Alpha 2", language: "pt-BR", retention_days: 90, vad: { silence_ms: 600 } })
  })

  test("anAgentWithAnyStoredSilenceCanBeSaved", async () => {
    const existing = agent({ vad: { silence_ms: 850 } })
    const fetchMock = fakeServer([existing])
    const f = form(existing, fetchMock)
    expect(f.state.silenceMs).toBe(850)
    expect(f.validate()).toBeNull()
    f.update({ displayName: "Renamed" })
    await f.save()
    // Silence did not change, so it is not sent; 851 is a value too.
    expect(JSON.parse(sent(fetchMock, "PATCH")[0]!.init.body as string)).toEqual({ display_name: "Renamed" })
    f.update({ silenceMs: 851 })
    expect(f.fields).toEqual({ display_name: "Renamed", vad: { silence_ms: 851 } })
    expect(f.validate()).toBeNull()
  })

  test("anEditThatChangedNothingSendsNothing", async () => {
    const fetchMock = fakeServer([])
    const f = form(agent(), fetchMock)
    expect((await f.save()).id).toBe("ag_a")
    expect(fetchMock.calls).toHaveLength(0)
  })

  test("formKeepsMaskedSecrets", async () => {
    const existing = agent({ stt: own })
    const fetchMock = fakeServer([existing])
    const f = form(existing, fetchMock)
    expect(f.state.sttApiKey).toBe("***")
    // Not changed: not sent, and not checked.
    expect(f.fields).toEqual({})
    expect(f.validate()).toBeNull()
    // Changed: the whole endpoint goes, the mask as it came.
    f.update({ sttModel: "whisper-2" })
    expect(f.validate()).toBeNull()
    expect(f.fields).toEqual({ stt: { ...own, model: "whisper-2" } })
    await f.save()
    expect(JSON.parse(sent(fetchMock, "PATCH")[0]!.init.body as string)).toEqual({ stt: { ...own, model: "whisper-2" } })
    // A new key replaces the mask; an empty one drops it.
    f.update({ sttApiKey: "sk-new" })
    expect(f.fields.stt).toMatchObject({ api_key: "sk-new" })
    f.update({ sttApiKey: "" })
    expect("api_key" in (f.fields.stt as object)).toBe(false)
  })

  test("aMaskedSecretOnlyCarriesOverToTheSameKindOfEndpoint", () => {
    const f = form(agent({ stt: { provider: "whisper" } }))
    f.update({ stt: "custom", sttBaseUrl: "https://stt.example/v1", sttModel: "m", sttApiKey: "***" })
    expect(f.validate()).toBe("Speech to text: enter the secret again, a stored one only carries over to the same kind of endpoint.")
    f.update({ sttApiKey: "real" })
    expect(f.validate()).toBeNull()
  })

  test("webhookFieldsAreCheckedAndKeepStoredHeaders", async () => {
    const hook = { type: "webhook", url: "https://hook.example/in", headers: { Authorization: "***", "X-Extra": "***" } }
    const existing = agent({ call_type: "one-shot", action: hook, tts: null })
    const f = form(existing)
    expect(f.state.webhookHeaderName).toBe("Authorization")
    expect(f.fields).toEqual({})
    f.update({ webhookUrl: "http://hook.example/in" })
    expect(f.validate()).toBe("Webhook: enter an https:// address without credentials, query or fragment.")
    f.update({ webhookUrl: "https://user:pw@hook.example/in" })
    expect(f.validate()).toBe("Webhook: enter an https:// address without credentials, query or fragment.")
    f.update({ webhookUrl: "https://hook.example/other" })
    expect(f.validate()).toBeNull()
    // The other stored header survives; the one shown is replaced.
    expect(f.fields.action).toEqual({ ...hook, url: "https://hook.example/other" })
    f.update({ webhookHeaderName: "Content-Type" })
    expect(f.validate()).toBe("Webhook: that header name is invalid or set by the server.")
    f.update({ webhookHeaderName: "X-Token", webhookHeaderValue: "" })
    expect(f.validate()).toBe("Webhook: a header needs a name and a value.")
    f.update({ webhookHeaderValue: "***" })
    expect(f.validate()).toBe("Webhook: enter the secret again, a stored one only carries over to the same kind of endpoint.")
    f.update({ webhookHeaderValue: "s3cret" })
    expect(f.validate()).toBeNull()
    expect(f.fields.action).toEqual({ type: "webhook", url: "https://hook.example/other", headers: { "X-Extra": "***", "X-Token": "s3cret" } })
  })

  test("switchingToOneWayDropsTheVoiceAndTheWebhookPickerStaysSeparate", () => {
    const f = form(agent())
    f.selectCallType("one-shot")
    // The responder does not fit a webhook step.
    expect(f.state.action).toBe("none")
    expect(f.validate()).toBe("Choose Webhook.")
    f.update({ action: "provider:notes-hook" })
    expect(f.validate()).toBeNull()
    expect(f.fields).toEqual({ call_type: "one-shot", action: { provider: "notes-hook" }, tts: null })
    expect(f.choices("action")).toEqual(["provider:notes-hook", "custom"])
  })

  test("customIsOfferedOnlyWhenTheServerAllowsIt", () => {
    expect(form(null, fakeServer([]), false).choices("stt")).toEqual(["none", "provider:whisper"])
    expect(form(null, fakeServer([]), true).choices("stt")).toEqual(["none", "provider:whisper", "custom"])
    const f = form(null, fakeServer([]), false)
    f.update({ displayName: "A", stt: "custom" })
    expect(f.validate()).toBe("Speech to text: this server doesn't accept custom endpoints.")
    // An edit offers no empty choice, and keeps a provider that is no longer offered.
    expect(form(agent({ stt: { provider: "gone" } }), fakeServer([]), false).choices("stt")).toEqual(["provider:whisper", "provider:gone"])
  })

  test("saveShowsTheServersRefusalAndReadsTheListOnSuccess", async () => {
    let saved = 0
    const refusing = fakeServer([], (method) => (method === "POST" ? ok({ error: "conflict", message: "an agent with slug 'notes' already exists" }, 409) : null))
    const f = new AgentFormModel(client(refusing), null, PROVIDERS, { onSaved: () => void saved++ })
    f.update({ displayName: "Notes" })
    await expect(f.save()).rejects.toThrow("an agent with slug 'notes' already exists")
    expect(f.state.saving).toBe(false)
    expect(saved).toBe(0)
    const accepting = new AgentFormModel(client(fakeServer([])), null, PROVIDERS, { onSaved: () => void saved++ })
    accepting.update({ displayName: "Notes" })
    await accepting.save()
    expect(saved).toBe(1)
    // Invalid forms are not sent.
    const invalid = fakeServer([])
    const empty = new AgentFormModel(client(invalid), null, PROVIDERS)
    await expect(empty.save()).rejects.toThrow("Slug:")
    expect(invalid.calls).toHaveLength(0)
  })
})
