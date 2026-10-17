import { fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import { ServerClient, type AgentDetail } from "../api/server"
import { ServersModel } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { mockFetch, type MockFetch } from "../test/mockFetch"
import { AgentsScreen } from "./AgentsView"
import { parseAgentsTarget, parseRoute } from "./route"

const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })

function agent(id: string, name: string, over: Partial<AgentDetail> = {}): AgentDetail {
  return {
    id, slug: name.toLowerCase(), display_name: name, icon: "brain", call_type: "conversation", turn_end: "auto", position: 0,
    language: "en", stt: { provider: "whisper" }, action: { provider: "claude" }, tts: { provider: "piper" }, system_prompt: "",
    fallback_message: "Sorry.", vad: { silence_ms: 800 }, timeouts: {}, retention_days: null, ...over,
  }
}

async function setup(agents: AgentDetail[]): Promise<{ servers: ServersModel; fetchMock: MockFetch }> {
  const fetchMock = mockFetch((url, init) => {
    if (url.endsWith("/v1/providers")) {
      return ok({ providers: [{ name: "whisper", kind: "stt" }, { name: "claude", kind: "action" }, { name: "piper", kind: "tts" }], custom_endpoints: false })
    }
    if (url.endsWith("/v1/agents") && init.method === "POST") return ok(agent("ag_new", "New"), 201)
    if (url.endsWith("/v1/agents")) return ok({ agents })
    if (init.method === "DELETE") {
      agents.splice(agents.findIndex((a) => url.endsWith(`/${a.id}`)), 1)
      return new Response(null, { status: 204 })
    }
    return ok({})
  })
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  await store.put({ id: "s1", name: "home", url: "https://home.example.com", token: "wc_pat_secret", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 })
  return { servers: new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, fetchMock)), fetchMock }
}

beforeEach(() => history.replaceState(null, "", "/servers/s1/agents"))
afterEach(() => history.replaceState(null, "", "/"))

describe("agents routes", () => {
  test("agentsRoutesParse", () => {
    expect(parseRoute("/servers/s1/agents")).toEqual({ tab: "servers", path: "/servers/s1/agents" })
    expect(parseRoute("/servers/s1/agents/new")).toEqual({ tab: "servers", path: "/servers/s1/agents/new" })
    expect(parseRoute("/servers/s1/agents/ag_a")).toEqual({ tab: "servers", path: "/servers/s1/agents/ag_a" })
    expect(parseRoute("/servers/s1/agents/ag_a/x")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseRoute("/servers/s1/devices")).toEqual({ tab: "servers", path: "/servers/s1/devices" })
    expect(parseRoute("/servers/s1/devices/code")).toEqual({ tab: "servers", path: "/servers/s1/devices/code" })
    expect(parseRoute("/servers/s1/devices/other")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseAgentsTarget("/servers/a%2Fb/agents")).toEqual({ serverId: "a/b", screen: "list" })
    expect(parseAgentsTarget("/servers/s1/agents/new")).toEqual({ serverId: "s1", screen: "new" })
    expect(parseAgentsTarget("/servers/s1/agents/ag_a")).toEqual({ serverId: "s1", screen: "edit", agentId: "ag_a" })
    expect(parseAgentsTarget("/servers/s1")).toBeNull()
  })
})

describe("AgentsScreen", () => {
  test("the list shows each agent's color dot", async () => {
    const { servers } = await setup([agent("ag_a", "Alpha", { color: "purple" }), agent("ag_b", "Beta", { position: 1 })])
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "list" }} />)
    await screen.findByText("Alpha")
    const dots = [...document.querySelectorAll(".agent-dot")].map((d) => d.className)
    expect(dots).toEqual(["agent-dot color-purple", "agent-dot color-mint"])
  })

  test("listsAgentsAndMovesOne", async () => {
    const { servers, fetchMock } = await setup([agent("ag_a", "Alpha"), agent("ag_b", "Beta", { position: 1, call_type: "monologue" })])
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "list" }} />)
    await screen.findByText("Alpha")
    expect(screen.getByText("Monologue")).toBeTruthy()
    expect(screen.getByRole("link", { name: /Alpha/ }).getAttribute("href")).toBe("/servers/s1/agents/ag_a")
    expect((screen.getByRole("button", { name: "Move Alpha up" }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole("button", { name: "Move Beta down" }) as HTMLButtonElement).disabled).toBe(true)
    await waitFor(() => expect(screen.getByRole("link", { name: "New agent" }).getAttribute("href")).toBe("/servers/s1/agents/new"))
    fireEvent.click(screen.getByRole("button", { name: "Move Beta up" }))
    await waitFor(() => expect(fetchMock.calls.some((c) => c.init.method === "PATCH")).toBe(true))
    const patch = fetchMock.calls.find((c) => c.init.method === "PATCH")!
    expect(patch.url).toBe("https://home.example.com/v1/agents/ag_b")
    expect(JSON.parse(patch.init.body as string)).toEqual({ position: 0 })
  })

  test("agentsScreenTellsTheAgendaWhichServerChanged", async () => {
    const { servers } = await setup([agent("ag_a", "Alpha"), agent("ag_b", "Beta", { position: 1 })])
    const seen: string[] = []
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "list" }} onAgentsChanged={(id, agents) => seen.push(`${id}:${agents.map((a) => a.id).join(",")}`)} />)
    await screen.findByText("Alpha")
    await waitFor(() => expect(seen).toEqual(["s1:ag_a,ag_b"]))
    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha" }))
    fireEvent.click(await screen.findByRole("button", { name: "Delete agent" }))
    await waitFor(() => expect(seen).toEqual(["s1:ag_a,ag_b", "s1:ag_b"]))
  })

  test("showsTheEmptyState", async () => {
    const { servers } = await setup([])
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "list" }} />)
    await screen.findByText("No agents")
  })

  test("deleteAsksFirst", async () => {
    const { servers, fetchMock } = await setup([agent("ag_a", "Alpha"), agent("ag_b", "Beta", { position: 1 })])
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "list" }} />)
    await screen.findByText("Alpha")
    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha" }))
    await screen.findByText(/Calls already in the history stay there/)
    expect(fetchMock.calls.some((c) => c.init.method === "DELETE")).toBe(false)
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    expect(screen.queryByText(/Calls already in the history/)).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha" }))
    fireEvent.click(screen.getByRole("button", { name: "Delete agent" }))
    await waitFor(() => expect(screen.queryByText("Alpha")).toBeNull())
    expect(screen.getByText("Beta")).toBeTruthy()
  })

  test("aRemovedServerSaysSo", async () => {
    const { servers } = await setup([])
    render(<AgentsScreen servers={servers} target={{ serverId: "gone", screen: "list" }} />)
    await screen.findByText("Server removed")
  })

  test("newAgentFormSavesAndGoesBackToTheList", async () => {
    const { servers, fetchMock } = await setup([])
    history.replaceState(null, "", "/servers/s1/agents/new")
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "new" }} />)
    const name = (await screen.findByLabelText("Name")) as HTMLInputElement
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.input(name, { target: { value: "Daily notes" } })
    await waitFor(() => expect((screen.getByLabelText("Slug") as HTMLInputElement).value).toBe("daily-notes"))
    fireEvent.click(screen.getByRole("radio", { name: "calendar" }))
    await waitFor(() => expect(save.disabled).toBe(false))
    // The call type decides which steps show.
    expect(screen.getByLabelText("Voice")).toBeTruthy()
    fireEvent.change(screen.getByLabelText("Call type"), { target: { value: "one-shot" } })
    await waitFor(() => expect(screen.queryByLabelText("Voice")).toBeNull())
    fireEvent.change(screen.getByLabelText("Call type"), { target: { value: "conversation" } })
    fireEvent.submit(save.form!)
    await waitFor(() => expect(location.pathname).toBe("/servers/s1/agents"))
    const post = fetchMock.calls.find((c) => c.init.method === "POST")!
    expect(JSON.parse(post.init.body as string)).toEqual({ slug: "daily-notes", display_name: "Daily notes", icon: "calendar", call_type: "conversation", turn_end: "auto", language: "en" })
  })

  test("editFormShowsTheRefusalAndKeepsTheSlugFixed", async () => {
    const { servers } = await setup([agent("ag_a", "Alpha")])
    history.replaceState(null, "", "/servers/s1/agents/ag_a")
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "edit", agentId: "ag_a" }} />)
    const name = (await screen.findByLabelText("Name")) as HTMLInputElement
    expect(name.value).toBe("Alpha")
    expect(screen.queryByLabelText("Slug")).toBeNull()
    expect(screen.getByText("alpha")).toBeTruthy()
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.input(name, { target: { value: "" } })
    await screen.findByText("Enter a name of up to 64 characters.")
  })

  test("editFormLetsTheModelDecideSilence", async () => {
    const { servers, fetchMock } = await setup([agent("ag_a", "Alpha", { vad: { silence_ms: 850 } })])
    history.replaceState(null, "", "/servers/s1/agents/ag_a")
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "edit", agentId: "ag_a" }} />)
    const name = (await screen.findByLabelText("Name")) as HTMLInputElement
    // The browser's own checks (silence step of 100, say) are off: the model validates.
    expect(name.form!.noValidate).toBe(true)
    fireEvent.input(name, { target: { value: "Alpha 2" } })
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement
    await waitFor(() => expect(save.disabled).toBe(false))
    fireEvent.submit(save.form!)
    await waitFor(() => expect(fetchMock.calls.some((c) => c.init.method === "PATCH")).toBe(true))
    expect(JSON.parse(fetchMock.calls.find((c) => c.init.method === "PATCH")!.init.body as string)).toEqual({ display_name: "Alpha 2" })
  })

  test("editFormWaitsForTheProvidersAndShowsTheirError", async () => {
    const failing = new ServersModel(
      { list: async () => [], get: async () => ({ id: "s1", name: "home", url: "https://home.example.com", token: "t", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 }), put: async () => {}, remove: async () => {}, update: async () => null },
      () => new ServerClient(new URL("https://home.example.com"), "t", mockFetch((url) => (url.endsWith("/v1/providers") ? ok({ error: "x", message: "The server is unavailable." }, 503) : ok({ agents: [agent("ag_a", "Alpha")] })))),
    )
    render(<AgentsScreen servers={failing} target={{ serverId: "s1", screen: "edit", agentId: "ag_a" }} />)
    await screen.findByText("The server is unavailable.")
    expect(screen.queryByLabelText("Name")).toBeNull()
  })

  test("anUnknownAgentSaysSo", async () => {
    const { servers } = await setup([agent("ag_a", "Alpha")])
    render(<AgentsScreen servers={servers} target={{ serverId: "s1", screen: "edit", agentId: "ag_zzz" }} />)
    await screen.findByText("This agent no longer exists.")
  })
})
